"""
sentinel_bridge.py
------------------
MQTT bridge between the ESP32 sensor publisher and the SentinelAI agent.

Subscribes to:
  sentinel/sentinel/cmd       -- manual overrides (buzzer, exhaust, load power)
  sentinel/sentinel/sensors   -- raw sensor readings from the ESP32

Publishes to:
  sentinel/sentinel/telemetry -- reasoning output for the dashboard
"""

import sys
import os
import json
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt
from reasoning.bayes import SentinelAI

# ------------------------------------------------------------------
# MQTT CONFIG
# ------------------------------------------------------------------
BROKER_HOST = "10.87.61.232"
BROKER_PORT = 1883
MQTT_USER   = "sentinel"
MQTT_PASS   = "87654321"

DEVICE_ID       = "sentinel"
TOPIC_TELEMETRY = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_COMMAND   = f"sentinel/{DEVICE_ID}/cmd"
TOPIC_SENSORS   = f"sentinel/{DEVICE_ID}/sensors"      # ← THE MISSING LINE

# ------------------------------------------------------------------
# AGENT
# ------------------------------------------------------------------
agent = SentinelAI()

manual = {
    "buzzer":     None,
    "exhaust":    None,
    "load_power": None,
}

# ------------------------------------------------------------------
# MQTT CLIENT
# ------------------------------------------------------------------
client = mqtt.Client(client_id="sentinel-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)


def on_connect(c, userdata, flags, rc):
    if rc == 0:
        print(f"[bridge] connected to {BROKER_HOST}:{BROKER_PORT}")
        c.subscribe(TOPIC_COMMAND)
        c.subscribe(TOPIC_SENSORS)
        print(f"[bridge] listening on {TOPIC_COMMAND} and {TOPIC_SENSORS}")
    else:
        print(f"[bridge] connect failed rc={rc}")


def on_message(c, userdata, msg):
    # ---- manual override ----
    if msg.topic == TOPIC_COMMAND:
        try:
            data = json.loads(msg.payload.decode())
        except Exception as e:
            print(f"[bridge] bad cmd JSON: {e}")
            return
        for k in manual:
            if k in data:
                manual[k] = bool(data[k])
        print(f"[bridge] manual override: {manual}")
        return

    # ---- raw sensor reading ----
    if msg.topic == TOPIC_SENSORS:
        try:
            reading = json.loads(msg.payload.decode())
        except Exception as e:
            print(f"[bridge] bad sensor JSON: {e}")
            return

        reading.setdefault("mq2", 0)
        reading.setdefault("mq135", 0)
        reading.setdefault("temp", 25)
        reading.setdefault("hum", 50)
        reading.setdefault("flame", 1)
        reading.setdefault("pir", 0)
        reading.setdefault("current", 0)

        try:
            agent_input = {
                "mq2":     float(reading["mq2"]),
                "mq135":   float(reading["mq135"]),
                "temp":    float(reading["temp"]),
                "hum":     float(reading["hum"]),
                "flame":   int(reading["flame"]),
                "pir":     int(reading["pir"]),
                "current": float(reading["current"]),
            }
        except (TypeError, ValueError) as e:
            print(f"[bridge] bad sensor types: {e}  payload={reading}")
            return

        result = agent.step(**agent_input)
        publish(agent_input, result)
        return


client.on_connect = on_connect
client.on_message = on_message


def start_mqtt():
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()


def publish(reading: dict, result: dict):
    level  = result["level"]
    hazard = result["hazard"]
    acts   = result["actions"]

    exhaust = acts.get("exhaust_fan", False)
    buzzer  = acts.get("buzzer", False)
    load_pw = acts.get("relay_power", True)

    if manual["buzzer"] is not None and level != "RED":
        buzzer = manual["buzzer"]
    if manual["exhaust"] is not None and not (level == "RED" and hazard == "FIRE"):
        exhaust = manual["exhaust"]
    if manual["load_power"] is not None and hazard != "OVERHEAT":
        load_pw = manual["load_power"]

    payload = {
        "timestamp":   int(time.time() * 1000),
        "device_id":   DEVICE_ID,
        "level":       level,
        "hazard":      hazard,
        "confidence":  result["confidence"],
        "risk_score":  result["risk_score"],
        "beliefs":     result["beliefs"],
        "rules_fired": result["rules_fired"],
        "actions": {
            "exhaust_fan": bool(exhaust),
            "buzzer":      bool(buzzer),
            "relay_power": bool(load_pw),
            "red_led":     acts.get("red_led",    False),
            "yellow_led":  acts.get("yellow_led", False),
            "green_led":   acts.get("green_led",  False),
        },
        "reading": reading,
    }

    client.publish(TOPIC_TELEMETRY, json.dumps(payload), qos=1)
    print(f"[bridge] → {level:6} {hazard:9} risk={result['risk_score']:5}  "
          f"fan={payload['actions']['exhaust_fan']} "
          f"buzzer={payload['actions']['buzzer']}")


if __name__ == "__main__":
    start_mqtt()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
        client.loop_stop()
        client.disconnect()
