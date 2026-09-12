"""
sentinel_bridge.py
------------------
Reads raw sensor + status JSON from the ESP32, runs it through the
SentinelAI Bayesian reasoning agent, and publishes the verdict to a
separate topic for the dashboard.

Topics:
  IN       sentinel/sentinel/telemetry   (from ESP32)
  OUT      sentinel/sentinel/agent       (to dashboard)
  COMMAND  sentinel/sentinel/cmd         (manual overrides, optional)
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
TOPIC_INPUT     = f"sentinel/{DEVICE_ID}/telemetry"   # ESP32 -> bridge
TOPIC_OUTPUT    = f"sentinel/{DEVICE_ID}/agent"       # bridge -> dashboard
TOPIC_COMMAND   = f"sentinel/{DEVICE_ID}/cmd"         # dashboard -> bridge

# ------------------------------------------------------------------
# AGENT — one persistent instance
# ------------------------------------------------------------------
agent = SentinelAI()

manual = {"buzzer": None, "exhaust": None, "load_power": None}

# ------------------------------------------------------------------
# MQTT CLIENT
# ------------------------------------------------------------------
client = mqtt.Client(client_id="sentinel-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)


def on_connect(c, userdata, flags, rc):
    if rc == 0:
        print(f"[bridge] connected to {BROKER_HOST}:{BROKER_PORT}")
        c.subscribe(TOPIC_INPUT)
        c.subscribe(TOPIC_COMMAND)
        print(f"[bridge] listening on {TOPIC_INPUT} and {TOPIC_COMMAND}")
        print(f"[bridge] publishing to {TOPIC_OUTPUT}")
    else:
        print(f"[bridge] connect failed rc={rc}")


# ------------------------------------------------------------------
# FIELD TRANSLATION: ESP32 -> SentinelAI.step() args
# ------------------------------------------------------------------
def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _i(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def translate(data: dict) -> dict:
    """
    ESP32 flat payload -> SentinelAI.step() keyword args.

    Field name mapping:
      temperature -> temp
      humidity    -> hum
      motion      -> pir
      flame       -> flame   (accepts 0/1 or raw ADC >1)

    flame semantics: 0 = flame detected, 1 = no flame (matches bayes.py).
    If raw ADC is received (>1), coerce by threshold.
    """
    raw_flame = data.get("flame", 1)
    if isinstance(raw_flame, (int, float)) and raw_flame > 1:
        flame = 0 if raw_flame > 2000 else 1
    else:
        flame = _i(raw_flame, 1)

    return {
        "mq2":     _f(data.get("mq2", 0)),
        "mq135":   _f(data.get("mq135", 0)),
        "temp":    _f(data.get("temperature", data.get("temp", 25))),
        "hum":     _f(data.get("humidity",    data.get("hum", 50))),
        "flame":   flame,
        "pir":     _i(data.get("motion",      data.get("pir", 0))),
        "current": _f(data.get("current", 0)),
    }


# ------------------------------------------------------------------
# MESSAGE HANDLER
# ------------------------------------------------------------------
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

    # ---- raw telemetry from ESP32 ----
    if msg.topic == TOPIC_INPUT:
        try:
            data = json.loads(msg.payload.decode())
        except Exception as e:
            print(f"[bridge] bad sensor JSON: {e}")
            return

        # Sanity: skip payloads that don't look like the ESP32's
        if "temperature" not in data and "temp" not in data:
            return

        reading = translate(data)

        try:
            result = agent.step(**reading)
        except Exception as e:
            print(f"[bridge] agent.step failed: {e}  reading={reading}")
            return

        publish(reading, result, raw=data)
        return


client.on_connect = on_connect
client.on_message = on_message


def start_mqtt():
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()


# ------------------------------------------------------------------
# PUBLISHER
# ------------------------------------------------------------------
def publish(reading, result, raw=None):
    level  = result["level"]
    hazard = result["hazard"]
    acts   = result["actions"]

    exhaust = acts.get("exhaust_fan", False)
    buzzer  = acts.get("buzzer", False)
    load_pw = acts.get("relay_power", True)

    # manual overrides (subject to safety interlocks)
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
            "red_led":     bool(acts.get("red_led",    False)),
            "yellow_led":  bool(acts.get("yellow_led", False)),
            "green_led":   bool(acts.get("green_led",  False)),
        },

        "reading": reading,
        "esp32_raw": raw or {},
    }

    client.publish(TOPIC_OUTPUT, json.dumps(payload), qos=1)
    print(f"[bridge] → {level:6} {hazard:9} risk={result['risk_score']:5}  "
          f"fan={payload['actions']['exhaust_fan']} "
          f"buzzer={payload['actions']['buzzer']}")


# ------------------------------------------------------------------
if __name__ == "__main__":
    start_mqtt()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
        client.loop_stop()
        client.disconnect()
