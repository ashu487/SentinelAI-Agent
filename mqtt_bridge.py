"""
MQTT <-> reasoning engine bridge

IMPORTANT:
- Keeps only the LATEST telemetry reading.
- Prevents old MQTT messages from building up and being processed later.
- ESP32 topics and telemetry format remain unchanged.
- AI reasoning remains in reasoning/bayes.py.
- Manual-vs-automatic arbitration is preserved.

Usage:
    pip install paho-mqtt requests
    python mqtt_bridge.py
"""

import json
import time
import threading
import requests
import paho.mqtt.client as mqtt


# ============================================================
# CONFIG
# ============================================================

DEVICE_ID = "sentinel"

BROKER_HOST = "10.87.61.232"
BROKER_PORT = 1883

MQTT_USER = "sentinel"
MQTT_PASS = "87654321"

TOPIC_TELEMETRY = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_COMMAND = f"sentinel/{DEVICE_ID}/cmd"

REASONING_API = "http://localhost:5000/api/sensor-data"

AUTO_PUBLISH_ACTIONS = True

MANUAL_OVERRIDE_SECONDS = 60


# ============================================================
# SHARED STATE
# ============================================================

_manual_override_until = 0.0

# This stores ONLY the newest telemetry reading.
_latest_telemetry = None

# Lock protects the variable because MQTT callback and
# processing loop access it from different threads.
_telemetry_lock = threading.Lock()


EXPECTED_TELEMETRY_KEYS = [
    "temperature",
    "humidity",
    "mq2",
    "mq135",
    "flame",
    "motion",
    "current"
]


# ============================================================
# TELEMETRY MAPPING
# ============================================================

def map_telemetry_to_reading(data: dict) -> dict:

    missing = [
        k for k in EXPECTED_TELEMETRY_KEYS
        if k not in data
    ]

    if missing:
        print(
            f"[bridge] WARNING: telemetry missing {missing} "
            f"-- raw payload: {data}"
        )

    return {
        "mq2": float(data.get("mq2", 0)),
        "mq135": float(data.get("mq135", 0)),

        "temp": float(
            data.get("temperature", 25)
        ),

        "hum": float(
            data.get("humidity", 50)
        ),

        # IMPORTANT:
        # Flame stays as raw ADC value.
        "flame": float(
            data.get("flame", 4000)
        ),

        "pir": 1 if data.get("motion") else 0,

        "current": float(
            data.get("current", 0)
        ),
    }


# ============================================================
# AI DECISION -> ESP32 COMMAND
# ============================================================

def map_decision_to_command(decision: dict) -> dict:

    actions = decision.get("actions", {})

    return {
        "origin": "auto",

        "buzzer": (
            1 if actions.get("buzzer") else 0
        ),

        "exhaust": (
            1 if actions.get("exhaust_fan") else 0
        ),

        "load_power": (
            1 if actions.get("relay_power") else 0
        ),

        "status_light": {
            "RED": "#ff0000",
            "YELLOW": "#ffcc00",
            "GREEN": "#00ff00",
        }.get(
            decision.get("level", "GREEN"),
            "#00ff00"
        ),

        "oled_message":
            f"{decision.get('hazard', 'SAFE')} "
            f"{decision.get('level', 'GREEN')}",
    }


# ============================================================
# MQTT CONNECT
# ============================================================

def on_connect(
    client,
    userdata,
    flags,
    rc,
    properties=None
):

    if rc == 0:

        print(
            f"[mqtt] connected to "
            f"{BROKER_HOST}:{BROKER_PORT}"
        )

        # Telemetry:
        # qos 0 + clean session means we don't want
        # a backlog of old telemetry.
        client.subscribe(
            TOPIC_TELEMETRY,
            qos=0
        )

        # Commands need to be monitored for manual override.
        client.subscribe(
            TOPIC_COMMAND,
            qos=1
        )

        print(
            f"[mqtt] subscribed to "
            f"{TOPIC_TELEMETRY} and "
            f"{TOPIC_COMMAND}"
        )

    else:

        print(
            f"[mqtt] connection failed, rc={rc}"
        )


# ============================================================
# COMMAND HANDLING
# ============================================================

def handle_command_message(data: dict):

    global _manual_override_until

    origin = data.get("origin")

    if origin == "manual":

        _manual_override_until = (
            time.time()
            + MANUAL_OVERRIDE_SECONDS
        )

        print(
            "[bridge] manual command detected -- "
            f"AI auto-actions paused for "
            f"{MANUAL_OVERRIDE_SECONDS}s "
            "(unless hazard goes RED)"
        )


# ============================================================
# MQTT MESSAGE CALLBACK
# ============================================================

def on_message(client, userdata, msg):

    global _latest_telemetry

    try:
        data = json.loads(
            msg.payload.decode()
        )

    except json.JSONDecodeError:

        print(
            f"[mqtt] non-JSON payload on "
            f"{msg.topic}: {msg.payload}"
        )

        return


    # --------------------------------------------------------
    # COMMAND MESSAGE
    # --------------------------------------------------------

    if msg.topic == TOPIC_COMMAND:

        handle_command_message(data)

        return


    # --------------------------------------------------------
    # TELEMETRY MESSAGE
    # --------------------------------------------------------

    if msg.topic != TOPIC_TELEMETRY:

        return


    # Convert ESP32 telemetry into AI format.
    reading = map_telemetry_to_reading(data)

    reading["device_id"] = DEVICE_ID


    # ========================================================
    # IMPORTANT FIX
    # ========================================================
    #
    # DO NOT call requests.post() here.
    #
    # Just replace the previous reading with this newest one.
    #
    # If ESP32 sends:
    #
    # 3.0 A
    # 2.9 A
    # 0.3 A
    #
    # only 0.3 A needs to be processed if it is the newest.
    # ========================================================

    with _telemetry_lock:

        _latest_telemetry = reading


# ============================================================
# PROCESS LATEST TELEMETRY
# ============================================================

def process_latest_telemetry(client):

    global _latest_telemetry

    while True:

        # ----------------------------------------------------
        # Get newest reading
        # ----------------------------------------------------

        with _telemetry_lock:

            if _latest_telemetry is None:

                reading = None

            else:

                reading = _latest_telemetry

                # Clear it.
                #
                # If another MQTT message arrives while the
                # API request is running, it will replace this
                # with a newer value.
                _latest_telemetry = None


        # ----------------------------------------------------
        # Nothing to process
        # ----------------------------------------------------

        if reading is None:

            time.sleep(0.01)

            continue


        # ----------------------------------------------------
        # SEND CURRENT READING TO AI
        # ----------------------------------------------------

        try:

            resp = requests.post(
                REASONING_API,
                json=reading,
                timeout=3
            )

            resp.raise_for_status()

            decision = resp.json()

        except requests.RequestException as e:

            print(
                f"[bridge] failed to reach "
                f"reasoning API: {e}"
            )

            continue

        except ValueError as e:

            print(
                f"[bridge] invalid JSON response "
                f"from reasoning API: {e}"
            )

            continue


        # ----------------------------------------------------
        # DISPLAY RESULT
        # ----------------------------------------------------

        print(
            f"[bridge] {reading} "
            f"-> hazard={decision.get('hazard')} "
            f"level={decision.get('level')} "
            f"risk={decision.get('risk_score')}"
        )


        # ----------------------------------------------------
        # AUTO ACTIONS
        # ----------------------------------------------------

        if not AUTO_PUBLISH_ACTIONS:

            continue


        manual_override_active = (
            time.time()
            < _manual_override_until
        )

        hazard_is_critical = (
            decision.get("level") == "RED"
        )


        # ----------------------------------------------------
        # MANUAL OVERRIDE
        # ----------------------------------------------------

        if (
            manual_override_active
            and not hazard_is_critical
        ):

            print(
                "[bridge] manual override active "
                "-- skipping auto-publish"
            )

            continue


        # ----------------------------------------------------
        # RED SAFETY OVERRIDE
        # ----------------------------------------------------

        if (
            manual_override_active
            and hazard_is_critical
        ):

            print(
                "[bridge] SAFETY OVERRIDE: "
                "hazard is RED -- "
                "auto-publishing despite "
                "active manual override"
            )


        # ----------------------------------------------------
        # CREATE COMMAND
        # ----------------------------------------------------

        cmd = map_decision_to_command(
            decision
        )


        # ----------------------------------------------------
        # SEND COMMAND TO ESP32
        # ----------------------------------------------------

        result = client.publish(
            TOPIC_COMMAND,
            json.dumps(cmd),
            qos=1
        )


        if result.rc == mqtt.MQTT_ERR_SUCCESS:

            print(
                f"[bridge] published auto command "
                f"-> {cmd}"
            )

        else:

            print(
                f"[bridge] failed to publish command "
                f"rc={result.rc}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    client = mqtt.Client(
        client_id="sentinel-reasoning-bridge",
        callback_api_version=
            mqtt.CallbackAPIVersion.VERSION2
    )

    client.username_pw_set(
        MQTT_USER,
        MQTT_PASS
    )

    client.on_connect = on_connect
    client.on_message = on_message


    print(
        f"[bridge] connecting to "
        f"{BROKER_HOST}:{BROKER_PORT} ..."
    )


    client.connect(
        BROKER_HOST,
        BROKER_PORT,
        keepalive=30
    )


    # ========================================================
    # Start processing thread
    # ========================================================

    processor = threading.Thread(
        target=process_latest_telemetry,
        args=(client,),
        daemon=True
    )

    processor.start()


    # ========================================================
    # MQTT NETWORK LOOP
    # ========================================================

    client.loop_forever()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()