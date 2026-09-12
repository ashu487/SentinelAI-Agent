"""
sentinel_bridge.py
------------------
The MQTT publisher. Imports the SentinelAI reasoning agent and pushes
each reasoning result to the MQTT broker as JSON.

Subscribes to:  (nothing required; add sentinel/<id>/cmd if you want
                 manual overrides from a dashboard command panel)

Publishes to:   sentinel/<DEVICE_ID>/telemetry
"""

import sys
import os
import json
import time

# Make the project root importable so `reasoning.bayes` resolves
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt
from reasoning.bayes import SentinelAI

# ------------------------------------------------------------------
# MQTT CONFIG — matches the dashboard
# ------------------------------------------------------------------
BROKER_HOST = "10.87.61.232"
BROKER_PORT = 1883                  # native MQTT (browser uses 9001)
MQTT_USER   = "sentinel"
MQTT_PASS   = "87654321"

DEVICE_ID       = "sentinel"
TOPIC_TELEMETRY = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_COMMAND   = f"sentinel/{DEVICE_ID}/cmd"

# ------------------------------------------------------------------
# MQTT CLIENT
# ------------------------------------------------------------------
client = mqtt.Client(client_id="sentinel-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)

# Manual overrides from an optional command panel (all None = defer to agent)
manual = {
    "buzzer":     None,
    "exhaust":    None,
    "load_power": None,
}

def on_connect(c, userdata, flags, rc):
    if rc == 0:
        print(f"[bridge] connected to {BROKER_HOST}:{BROKER_PORT}")
        c.subscribe(TOPIC_COMMAND)
        print(f"[bridge] listening on {TOPIC_COMMAND}")
    else:
        print(f"[bridge] connect failed rc={rc}")

def on_message(c, userdata, msg):
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
LEVEL_COLOR = {
    "GREEN":  "#2f6d4f",
    "YELLOW": "#b3791f",
    "RED":    "#b3311f",
}

def _oled(level, hazard):
    return f"{level[:4]} {hazard[:15]}"[:21]


def publish(reading: dict, result: dict):
    """
    reading : {"mq2", "mq135", "temp", "hum", "flame", "pir", "current"}
    result  : the dict returned by SentinelAI.step()
    """
    level  = result["level"]
    hazard = result["hazard"]
    acts   = result["actions"]

    # Safety interlocks — agent wins over manual input
    exhaust   = acts.get("exhaust_fan", False)
    buzzer    = acts.get("buzzer", False)
    load_pw   = acts.get("relay_power", True)

    if manual["buzzer"]     is not None and level != "RED":
        buzzer  = manual["buzzer"]
    if manual["exhaust"]    is not None and not (level == "RED" and hazard == "FIRE"):
        exhaust = manual["exhaust"]
    if manual["load_power"] is not None and hazard != "OVERHEAT":
        load_pw = manual["load_power"]

    payload = {
        "timestamp":    int(time.time() * 1000),
        "device_id":    DEVICE_ID,

        # ---- reasoning output ----
        "level":        level,
        "hazard":       hazard,
        "confidence":   result["confidence"],
        "risk_score":   result["risk_score"],
        "beliefs":      result["beliefs"],
        "rules_fired":  result["rules_fired"],

        # ---- actuator commands (dashboard reads these) ----
        "actions": {
            "exhaust_fan": bool(exhaust),
            "buzzer":      bool(buzzer),
            "relay_power": bool(load_pw),
            "red_led":     acts.get("red_led",    False),
            "yellow_led":  acts.get("yellow_led", False),
            "green_led":   acts.get("green_led",  False),
        },

        # ---- raw reading (dashboard shows this in the sensors panel) ----
        "reading": reading,
    }

    client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)
    print(f"[bridge] → {level:6} {hazard:9} risk={result['risk_score']:5}  "
          f"fan={payload['actions']['exhaust_fan']} "
          f"buzzer={payload['actions']['buzzer']}")
    return payload


# ------------------------------------------------------------------
# SIMULATOR — cycles through scenarios so you can watch the dashboard
# ------------------------------------------------------------------
SCENARIOS = [
    # label         mq2  mq135 temp hum flm pir cur
    ("normal",     1200,  500, 28, 55, 1, 1, 0.4),
    ("normal",     1250,  520, 29, 54, 1, 1, 0.4),
    ("gas leak",   2200, 1500, 30, 60, 1, 0, 0.3),
    ("gas leak",   2400, 1700, 31, 61, 1, 0, 0.3),
    ("overheat",   1300,  700, 65, 40, 1, 1, 2.5),
    ("overheat",   1350,  720, 68, 39, 1, 1, 2.6),
    ("fire",       2600, 2200, 80, 30, 0, 0, 1.8),
    ("fire",       2700, 2300, 82, 29, 0, 0, 1.9),
    ("recovering", 1500,  800, 45, 45, 1, 0, 0.6),
    ("normal",     1200,  500, 28, 55, 1, 1, 0.4),
]


def run_simulator(interval=3.0):
    agent = SentinelAI()
    print(f"[bridge] simulator started — publishing to {TOPIC_TELEMETRY}\n")
    i = 0
    while True:
        label, mq2, mq135, temp, hum, flame, pir, current = SCENARIOS[i % len(SCENARIOS)]

        reading = {
            "mq2": mq2, "mq135": mq135,
            "temp": temp, "hum": hum,
            "flame": flame, "pir": pir,
            "current": current,
        }
        result = agent.step(**reading)
        publish(reading, result)

        i += 1
        time.sleep(interval)


# ------------------------------------------------------------------
if __name__ == "__main__":
    start_mqtt()
    time.sleep(1)          # give MQTT a moment to connect
    try:
        run_simulator()
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
        client.loop_stop()
        client.disconnect()
