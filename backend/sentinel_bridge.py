import json
import os
import sys                                                    # ← add
import threading
import time

import paho.mqtt.client as mqtt

# allow `from reasoning.bayes import ...` when run as
# `python backend/sentinel_bridge.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # ← add

from reasoning.bayes import (Calibrator, SentinelAI, BASELINE_FILE,
                             load_profile, save_profile)

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
BROKER_HOST = os.environ.get("SENTINEL_BROKER", "172.20.10.10")
BROKER_PORT = 1883
MQTT_USER   = "sentinel"
MQTT_PASS   = "289961"

DEVICE_ID      = "sentinel"
TOPIC_IN       = f"sentinel/{DEVICE_ID}/telemetry"
TOPIC_CMD      = f"sentinel/{DEVICE_ID}/cmd"
TOPIC_AGENT    = f"sentinel/{DEVICE_ID}/agent"
TOPIC_OVERRIDE = f"sentinel/{DEVICE_ID}/override"

CAL_SAMPLES     = 120    # 60 s at 2 Hz (ESP32 publishes every 500 ms)
HEARTBEAT_S     = 5.0    # re-send actuator state; ESP32 forgets everything on reboot
OVERRIDE_TTL_S  = 120.0  # manual overrides expire unless re-sent

LEVEL_RGB = {"GREEN": "#00FF00", "YELLOW": "#FFFF00", "RED": "#FF0000"}
HAZARD_TEXT = {"SAFE": "All clear", "GAS_LEAK": "GAS LEAK",
               "OVERHEAT": "OVERHEAT", "FIRE": "FIRE!"}

# ------------------------------------------------------------------
# STATE (guarded by `lock`)
# ------------------------------------------------------------------
lock = threading.Lock()
agent = None                 # SentinelAI once calibrated
cal = None                   # Calibrator while calibrating
cal_count = 0
last_cmd = None              # last actuator command sent to ESP32
last_cmd_sent = 0.0
last_telemetry = 0.0
overrides = {}               # key -> (value, monotonic timestamp)

# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def to_sample(d):
    """ESP32 telemetry JSON -> reasoning inputs. Flame stays RAW ADC (do not binarise)."""
    def f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return {
        "mq2":     f(d.get("mq2", 0)),
        "mq135":   f(d.get("mq135", 0)),
        "temp":    f(d.get("temperature", d.get("temp", 25))),
        "hum":     f(d.get("humidity", d.get("hum", 50))),
        "flame":   f(d.get("flame", 4095), 4095),
        "pir":     int(f(d.get("motion", d.get("pir", 0)))),
        "current": f(d.get("current", 0)),
    }


def apply_overrides(cmd, level, hazard):
    """Manual overrides keep the safety rules from v3 (no buzzer-off in RED, etc.)."""
    now = time.monotonic()
    active = {k: v for k, (v, t) in overrides.items() if now - t < OVERRIDE_TTL_S}
    if "buzzer" in active and level != "RED":
        cmd["buzzer"] = active["buzzer"]
    if "exhaust" in active and not (level == "RED" and hazard == "FIRE"):
        cmd["exhaust"] = active["exhaust"]
    if "load_power" in active and hazard != "OVERHEAT":
        cmd["load_power"] = active["load_power"]
    return cmd


def build_cmd(result):
    """Reasoning result -> the exact JSON keys the ESP32 firmware reads from cmd."""
    acts = result["actions"]
    level, hazard = result["level"], result["hazard"]
    cmd = {
        "exhaust":      int(acts["exhaust_fan"]),
        "buzzer":       int(acts["buzzer"]),
        "sprinkler":    int(acts["sprinkler"]),
        "load_power":   int(acts["relay_power"]),
        "status_light": LEVEL_RGB[level],
        "oled_message": f"{level}: {HAZARD_TEXT[hazard]}",
        "source":      "agent",     # lets the bridge ignore its own cmd echoes
    }
    return apply_overrides(cmd, level, hazard)


def send_cmd(cmd):
    global last_cmd, last_cmd_sent
    client.publish(TOPIC_CMD, json.dumps(cmd), qos=1)
    last_cmd = cmd
    last_cmd_sent = time.monotonic()

# ------------------------------------------------------------------
# TELEMETRY HANDLER
# ------------------------------------------------------------------
def handle_telemetry(data):
    global agent, cal, cal_count, last_telemetry
    s = to_sample(data)
    with lock:
        last_telemetry = time.monotonic()

        # ---- calibration: collect quiet samples, publish nothing ----
        if agent is None:
            if cal is None:
                cal = Calibrator()
                cal_count = 0
                print("[bridge] calibrating: keep the room quiet for 60 s")
            cal.add(s)
            cal_count += 1
            if cal_count % 20 == 0:
                print(f"[bridge] calibrating {cal_count}/{CAL_SAMPLES} "
                      f"kept={len(cal.samples)} rejected={cal.rejected}")
            if cal_count >= CAL_SAMPLES:
                try:
                    profile = cal.finish()
                    save_profile(profile, n_samples=len(cal.samples), rejected=cal.rejected)
                    agent = SentinelAI(profile=profile)
                    print(f"[bridge] calibration done -> {BASELINE_FILE}")
                except RuntimeError as e:
                    print(f"[bridge] calibration failed: {e}. Restarting window.")
                cal, cal_count = None, 0
            return

        # ---- running: reason, then drive actuators ----
        result = agent.step(**s)
        agent_payload = dict(result, device_id=DEVICE_ID,
                             timestamp=int(time.time() * 1000), reading=s)
        client.publish(TOPIC_AGENT, json.dumps(agent_payload), qos=1)

        cmd = build_cmd(result)
        if cmd != last_cmd or time.monotonic() - last_cmd_sent > HEARTBEAT_S:
            send_cmd(cmd)

        print(f"[bridge] {result['level']:6} {result['hazard']:9} "
              f"risk={result['risk_score']:5}  exhaust={cmd['exhaust']} "
              f"load={cmd['load_power']} buzzer={cmd['buzzer']} sprinkler={cmd['sprinkler']}")


def handle_override(data):
    now = time.monotonic()
    with lock:
        if data.get("clear"):
            overrides.clear()
            print("[bridge] overrides cleared")
            return
        for key in ("buzzer", "exhaust", "load_power"):
            if key in data:
                if data[key] is None:
                    overrides.pop(key, None)
                else:
                    overrides[key] = (int(bool(data[key])), now)
        print(f"[bridge] overrides: {overrides}")

# ------------------------------------------------------------------
# MQTT
# ------------------------------------------------------------------
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        print(f"[bridge] connect failed: {reason_code}")
        return
    print(f"[bridge] connected to {BROKER_HOST}:{BROKER_PORT}")
    client.subscribe([(TOPIC_IN, 1), (TOPIC_CMD, 1), (TOPIC_OVERRIDE, 1)])


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except (ValueError, UnicodeDecodeError):
        print(f"[bridge] bad JSON on {msg.topic}")
        return
    if not isinstance(data, dict):
        return

    if msg.topic == TOPIC_IN:
        if "temperature" in data or "temp" in data:
            handle_telemetry(data)
    elif msg.topic == TOPIC_OVERRIDE:
        handle_override(data)
    elif msg.topic == TOPIC_CMD and data.get("source") != "agent":
        handle_override(data)   # a dashboard that still writes to cmd still works


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sentinel-bridge")
client.username_pw_set(MQTT_USER, MQTT_PASS)
client.on_connect = on_connect
client.on_message = on_message

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    profile = load_profile(BASELINE_FILE)
    if profile is not None:
        agent = SentinelAI(profile=profile)
        print(f"[bridge] loaded baseline from {BASELINE_FILE}")
    else:
        print("[bridge] no valid baseline; will calibrate from live telemetry")

    while True:
        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
            break
        except OSError as e:
            print(f"[bridge] cannot reach broker {BROKER_HOST}: {e}; retrying in 3 s")
            time.sleep(3)

    client.loop_start()           # network thread; auto-reconnects on drop
    stale_warned = False
    try:
        while True:
            time.sleep(1)
            with lock:
                # Re-send the current actuator state so the ESP32 stays in sync
                # even if it rebooted or a message was lost.
                if last_cmd is not None and time.monotonic() - last_cmd_sent > HEARTBEAT_S:
                    send_cmd(last_cmd)
                stale = agent is not None and time.monotonic() - last_telemetry > 5
                if stale and not stale_warned:
                    print("[bridge] WARNING: no telemetry for 5 s; actuators hold last state")
                stale_warned = stale
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
    finally:
        client.loop_stop()
        client.disconnect()
