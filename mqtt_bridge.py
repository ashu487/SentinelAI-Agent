"""
MQTT <-> reasoning engine bridge, with manual/automatic actuator arbitration.

Confirmed with hardware teammate:
- The ESP32 has NO onboard safety logic -- it only reports raw sensor data
  and executes whatever command it receives on the cmd topic. All hazard
  reasoning genuinely lives in reasoning/bayes.py. Good -- no competing
  brain to worry about.
- He asked the AI side to own the manual-vs-automatic conflict resolution
  policy. That's implemented below.

ARBITRATION POLICY (explain this in your report -- it's a real safety-
engineering decision, not just plumbing):
  1. Under normal conditions, a human's manual dashboard command wins.
     If someone just used a toggle, the AI backs off from auto-publishing
     for MANUAL_OVERRIDE_SECONDS, so it doesn't immediately fight the
     human and flip a switch back.
  2. The AI ALWAYS overrides manual control the instant the hazard is
     confirmed RED (critical) -- a manual mistake or a stale "off" toggle
     must never be able to block a real fire/gas-leak response. Safety-
     critical automatic action takes priority over convenience.
  3. Once the hazard drops back below RED, manual override resumes
     respecting the normal grace-period rule above.

This requires ONE small addition to dashboard.html (ask your teammate to
add it): the dashboard should tag its own published commands with
"origin": "manual" so this bridge can tell a human command apart from
its own auto-published ones (which are tagged "origin": "auto"). Single
line to add in his sendBtn click handler, right before JSON.stringify:
    cmd.origin = "manual";

Usage:
    pip install paho-mqtt requests
    python mqtt_bridge.py
"""

import json
import time
import requests
import paho.mqtt.client as mqtt

# ---------------- CONFIG (match dashboard.html's values) ----------------
DEVICE_ID = "sentinel"

BROKER_HOST = "10.87.61.232"   # same host as ws://10.87.61.232:9001 in dashboard.html
BROKER_PORT = 1883             # confirm this is the broker's plain MQTT port
                                # (9001 in dashboard.html is the WEBSOCKET port
                                # for browsers -- paho-mqtt here needs plain TCP)
MQTT_USER = "sentinel"
MQTT_PASS = "87654321"

TOPIC_TELEMETRY = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_COMMAND = f"sentinel/{DEVICE_ID}/cmd"

REASONING_API = "http://localhost:5000/api/sensor-data"

AUTO_PUBLISH_ACTIONS = True       # confirmed ready by the team
MANUAL_OVERRIDE_SECONDS = 60      # how long a human command "wins" before
                                   # the AI resumes auto-publishing, UNLESS
                                   # the hazard is RED (see arbitration below)

# ---------------- arbitration state ----------------
_manual_override_until = 0.0      # epoch seconds; 0 = no active manual override


EXPECTED_TELEMETRY_KEYS = ["temperature", "humidity", "mq2", "mq135", "flame", "motion", "current"]


def map_telemetry_to_reading(data: dict) -> dict:
    """
    Translate the ESP32's telemetry JSON shape into the field names
    reasoning/bayes.py's SentinelAI.step() expects.

    His telemetry: temperature, humidity, mq2, mq135,
                   flame (raw analog ADC, ~4000=no flame, drops as flame nears),
                   motion (bool, True=occupied), current
    bayes.py wants: temp, hum, mq2, mq135, flame (same raw analog value,
                     passed straight through -- extract_features() in
                     bayes.py does the normalization), pir, current

    NOTE: flame changed from a boolean digital signal to a raw analog
    reading. It MUST be passed through as a number here, not converted to
    0/1. The old `0 if data.get("flame") else 1` line would be a live bug
    now: any nonzero analog value (realistically always true, e.g. ~4000)
    is truthy in Python, so it would report "flame detected" on every
    single reading regardless of the actual value.

    Logs a warning whenever a message is missing expected keys, instead of
    silently substituting defaults -- this surfaces malformed/partial
    telemetry messages (e.g. a second publisher on the same topic, or a
    heartbeat message with no sensor payload) rather than hiding them as
    what looks like a normal "everything is fine" reading.
    """
    missing = [k for k in EXPECTED_TELEMETRY_KEYS if k not in data]
    if missing:
        print(f"[bridge] WARNING: telemetry message missing keys {missing} "
              f"-- raw payload: {data}. Defaulting the missing fields, but "
              f"this reading should NOT be trusted as a real sensor state. "
              f"Check with your hardware teammate what's publishing this.")

    return {
        "mq2": float(data.get("mq2", 0)),
        "mq135": float(data.get("mq135", 0)),
        "temp": float(data.get("temperature", 25)),
        "hum": float(data.get("humidity", 50)),
        "flame": float(data.get("flame", 4000)),  # raw analog, pass straight through
        "pir": 1 if data.get("motion") else 0,
        "current": float(data.get("current", 0)),
    }


def map_decision_to_command(decision: dict) -> dict:
    """
    Translate the reasoning engine's action plan into the command shape
    the ESP32 firmware expects, tagged as an automatic command so the
    dashboard (and this bridge, on its own echoed messages) can tell it
    apart from a manual one.

    Note: bayes.py's decide_actions() has no "sprinkler" action yet --
    the dashboard has a sprinkler toggle the AI never sets. If you want
    fire-triggered sprinkler activation, add it to decide_actions()
    deliberately (e.g. sprinkler=True only for FIRE at RED) -- discuss
    with your team first, it's a real physical actuator.
    """
    actions = decision.get("actions", {})
    return {
        "origin": "auto",
        "buzzer": 1 if actions.get("buzzer") else 0,
        "exhaust": 1 if actions.get("exhaust_fan") else 0,
        "load_power": 1 if actions.get("relay_power") else 0,
        "status_light": {
            "RED": "#ff0000",
            "YELLOW": "#ffcc00",
            "GREEN": "#00ff00",
        }.get(decision.get("level", "GREEN"), "#00ff00"),
        "oled_message": f"{decision.get('hazard', 'SAFE')} {decision.get('level', 'GREEN')}",
    }


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print(f"[mqtt] connected to {BROKER_HOST}:{BROKER_PORT}")
        client.subscribe(TOPIC_TELEMETRY)
        client.subscribe(TOPIC_COMMAND)   # also listen for manual commands, for arbitration
        print(f"[mqtt] subscribed to {TOPIC_TELEMETRY} and {TOPIC_COMMAND}")
    else:
        print(f"[mqtt] connection failed, rc={rc}")


def handle_command_message(data: dict):
    """
    Watches the command topic for MANUAL commands (origin == "manual",
    the tag his dashboard needs to add -- see module docstring) and starts
    a grace period during which the AI won't auto-publish, so it doesn't
    immediately fight a human's toggle.

    Commands the bridge published itself are tagged "origin": "auto" and
    are ignored here (otherwise the bridge would perpetually "override"
    its own messages).
    """
    global _manual_override_until

    origin = data.get("origin")
    if origin == "manual":
        _manual_override_until = time.time() + MANUAL_OVERRIDE_SECONDS
        print(f"[bridge] manual command detected -- AI auto-actions paused "
              f"for {MANUAL_OVERRIDE_SECONDS}s (unless hazard goes RED)")
    # origin == "auto" (or missing/unknown) -> not a manual command, ignore


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        print(f"[mqtt] non-JSON payload on {msg.topic}: {msg.payload}")
        return

    if msg.topic == TOPIC_COMMAND:
        handle_command_message(data)
        return

    if msg.topic != TOPIC_TELEMETRY:
        return

    reading = map_telemetry_to_reading(data)
    reading["device_id"] = DEVICE_ID

    try:
        resp = requests.post(REASONING_API, json=reading, timeout=3)
        resp.raise_for_status()
        decision = resp.json()
    except requests.RequestException as e:
        print(f"[bridge] failed to reach reasoning API: {e}")
        return

    print(f"[bridge] {reading} -> hazard={decision['hazard']} "
          f"level={decision['level']} risk={decision['risk_score']}")

    if not AUTO_PUBLISH_ACTIONS:
        return

    manual_override_active = time.time() < _manual_override_until
    hazard_is_critical = decision.get("level") == "RED"

    # Arbitration: manual control wins UNLESS the hazard is confirmed
    # critical, in which case safety-critical automatic action always
    # takes priority, no matter how recently a human touched a toggle.
    if manual_override_active and not hazard_is_critical:
        print("[bridge] manual override active -- skipping auto-publish")
        return

    if manual_override_active and hazard_is_critical:
        print("[bridge] SAFETY OVERRIDE: hazard is RED -- "
              "auto-publishing despite active manual override")

    cmd = map_decision_to_command(decision)
    client.publish(TOPIC_COMMAND, json.dumps(cmd), qos=1)
    print(f"[bridge] published auto command -> {cmd}")


def main():
    client = mqtt.Client(client_id="sentinel-reasoning-bridge",
                          callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[bridge] connecting to {BROKER_HOST}:{BROKER_PORT} ...")
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_forever()


if __name__ == "__main__":
    main()
