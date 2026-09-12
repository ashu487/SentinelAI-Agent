"""
sentinel_bridge.py
------------------
Publishes SentinelAI agent output to the MQTT dashboard.

Subscribes to:  (none — driven by SentinelAI's own reasoning loop)
Publishes to:   sentinel/sentinel/telemetry
Also listens on: sentinel/sentinel/cmd  (so dashboard toggles affect agent actions)
"""

import json
import time
import threading
import paho.mqtt.client as mqtt

# ------------------------------------------------------------------
# MQTT CONFIG  (matches your dashboard)
# ------------------------------------------------------------------
BROKER_HOST = "10.87.61.232"
BROKER_PORT = 1883                 # native MQTT; browser uses 9001 (websockets)
MQTT_USER   = "sentinel"
MQTT_PASS   = "87654321"

DEVICE_ID       = "sentinel"
TOPIC_TELEMETRY = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_COMMAND   = f"sentinel/{DEVICE_ID}/cmd"

# ------------------------------------------------------------------
# MAP SentinelAI level -> dashboard status color
# ------------------------------------------------------------------
LEVEL_COLOR = {
    "GREEN":  "#2f6d4f",
    "YELLOW": "#b3791f",
    "RED":    "#b3311f",
}

# 21-char max for OLED
def oled_text(level, hazard):
    return f"{level[:4]} {hazard[:15]}"[:21]

# ------------------------------------------------------------------
# MQTT CLIENT
# ------------------------------------------------------------------
client = mqtt.Client(client_id="sentinel-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)

# Latest manual override from the dashboard (via /cmd)
manual = {
    "buzzer":    None,
    "sprinkler": None,
    "exhaust":   None,
    "load_power": None,
}

def on_connect(c, userdata, flags, rc):
    if rc == 0:
        print(f"[bridge] connected to {BROKER_HOST}")
        c.subscribe(TOPIC_COMMAND)
        print(f"[bridge] listening on {TOPIC_COMMAND}")
    else:
        print(f"[bridge] connect failed rc={rc}")

def on_message(c, userdata, msg):
    """Handle manual commands from the dashboard."""
    try:
        data = json.loads(msg.payload.decode())
    except Exception as e:
        print(f"[bridge] bad cmd JSON: {e}")
        return

    for k in manual:
        if k in data:
            manual[k] = bool(data[k])

    print(f"[bridge] manual override: {manual}")

client.on_connect = on_connect
client.on_message = on_message

def start_mqtt():
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

# ------------------------------------------------------------------
# TELEMETRY PUBLISHER
# ------------------------------------------------------------------
def publish_telemetry(reading: dict, result: dict, sensors: dict):
    """
    reading  : raw sensor readings {mq2, mq135, temp, hum, flame, pir, current}
    result   : SentinelAI output {level, hazard, confidence, risk_score,
                                  beliefs, rules_fired, actions}
    sensors  : optional extra actuators e.g. {"sprinkler": False}
    """
    level  = result["level"]
    hazard = result["hazard"]
    acts   = result["actions"]

    # Manual override wins for load_power; automatic wins for safety outputs
    load_power = manual["load_power"] if manual["load_power"] is not None \
                 else acts.get("relay_power", True)

    # In a FIRE, exhaust must stay OFF for safety — never let dashboard override it
    exhaust = acts.get("exhaust_fan", False)
    if level == "RED" and hazard == "FIRE":
        exhaust = False

    payload = {
        # ---- raw sensors ----
        "temperature":  round(float(reading["temp"]), 1),
        "humidity":     round(float(reading["hum"]), 1),
        "mq2":          int(reading["mq2"]),
        "mq135":        int(reading["mq135"]),
        "flame":        bool(reading["flame"] == 0),      # sensor is active-low
        "motion":       bool(reading["pir"] == 1),
        "current":      round(float(reading["current"]), 2),

        # ---- actuators (agent decisions) ----
        "load_power":   bool(load_power),
        "exhaust":      bool(exhaust),
        "sprinkler":    bool(sensors.get("sprinkler", manual.get("sprinkler") or False)),
        "buzzer":       bool(acts.get("buzzer", False)),

        # ---- visualization ----
        "status_light": LEVEL_COLOR.get(level, "#000000"),
        "oled_message": oled_text(level, hazard),

        # ---- extra: agent reasoning, dashboard ignores unknown keys ----
        "risk_score":   result.get("risk_score"),
        "confidence":   result.get("confidence"),
        "beliefs":      result.get("beliefs"),
        "rules_fired":  result.get("rules_fired", []),
        "hazard":       hazard,
    }

    client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)
    print(f"[bridge] → {level} {hazard} risk={result.get('risk_score')}")
    return payload

# ------------------------------------------------------------------
# DEMO — run standalone to see it work
# ------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")                  # so we can import sentinelai
    try:
        from sentinelai import SentinelAI    # your earlier Python class
    except ImportError:
        print("Could not import SentinelAI. Ensure sentinelai.py is in this folder.")
        sys.exit(1)

    agent = SentinelAI()
    start_mqtt()
    time.sleep(1)

    print("\n[bridge] running demo cycles — Ctrl+C to stop\n")

    # cycle through scenarios repeatedly so you can see the dashboard react
    scenarios = [
        # mq2, mq135, temp, hum, flame, pir, current
        (1200,  500, 28, 55, 1, 1, 0.4),   # GREEN
        (2200, 1500, 30, 60, 1, 0, 0.3),   # YELLOW — gas
        (1300,  700, 65, 40, 1, 1, 2.5),   # YELLOW — overheat
        (2600, 2200, 80, 30, 0, 0, 1.8),   # RED   — fire
    ]

    i = 0
    while True:
        s = scenarios[i % len(scenarios)]
        reading = {"mq2": s[0], "mq135": s[1], "temp": s[2], "hum": s[3],
                   "flame": s[4], "pir": s[5], "current": s[6]}
        result = agent.step(*s)
        publish_telemetry(reading, result, sensors={"sprinkler": False})
        i += 1
        time.sleep(4)
