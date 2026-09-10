"""
SentinelAI bridge.
- Receives raw sensor readings from ESP32 (or the simulator) at
  POST /api/sensor-data, runs them through the SentinelAI reasoning
  agent, persists + broadcasts the result, and returns the actuator
  actions in the response.
- Also keeps /ingest as an alternative entry point for a payload that
  has ALREADY been fully reasoned about elsewhere (e.g. if you later
  move the reasoning onto the ESP32 itself in C++ and just want the
  dashboard to display its output).
- Pushes live updates to browsers via SSE at /stream.
- Serves the dashboard from dashboard/templates + dashboard/static.
- Persists events to backend/data/events.json.
"""

import sys
import os
import json
import time
import queue
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS

from reasoning.bayes import SentinelAI

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DATA_DIR = os.path.join(BASE, "data")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(ROOT, "dashboard", "templates"),
    static_folder=os.path.join(ROOT, "dashboard", "static"),
    static_url_path="/static",
)
CORS(app)

# One SentinelAI agent per device -- it maintains rolling belief state
# (beliefs, last_hazard, confirmation) across readings, so it must persist
# between requests rather than being recreated each time.
_agents: dict[str, SentinelAI] = {}


def get_agent(device_id: str) -> SentinelAI:
    if device_id not in _agents:
        _agents[device_id] = SentinelAI()
    return _agents[device_id]


# ---------- in-memory pub/sub (SSE) ----------
subscribers = []          # list[queue.Queue]
lock = threading.Lock()


def broadcast(payload: dict):
    with lock:
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


# ---------- persistence ----------
def load_events():
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_event(evt: dict):
    events = load_events()
    events.append(evt)
    events = events[-500:]  # keep last 500
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(events, f, indent=2)
    os.replace(tmp, EVENTS_FILE)


def _build_payload(device_id: str, reading: dict, result: dict) -> dict:
    return {
        "timestamp": time.time() * 1000,  # ms, matches new Date(timestamp) in script.js
        "device_id": device_id,
        "level": result["level"],
        "hazard": result["hazard"],
        "confidence": result["confidence"],
        "risk_score": result["risk_score"],
        "beliefs": result["beliefs"],
        "rules_fired": result["rules_fired"],
        "actions": result["actions"],
        "reading": reading,
    }


# ---------- routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sensor-data", methods=["POST"])
def ingest_sensor_data():
    """
    The endpoint the ESP32 (or sensor_simulator.py) POSTs RAW readings to.
    Expected JSON body:
    {
      "device_id": "lab-esp32-1",
      "mq2": 1200, "mq135": 500, "temp": 28, "hum": 55,
      "flame": 1, "pir": 1, "current": 0.4
    }
    flame: 0 = flame detected (active-low), 1 = no flame
    pir:   1 = human present, 0 = absent

    This is where the actual reasoning happens -- runs the reading
    through reasoning.bayes.SentinelAI, logs + broadcasts the full
    result, and returns the actuator actions so the ESP32 knows what
    to do.
    """
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id", "esp32-01")

    reading = {
        "mq2": float(data.get("mq2", 0)),
        "mq135": float(data.get("mq135", 0)),
        "temp": float(data.get("temp", 25)),
        "hum": float(data.get("hum", 50)),
        "flame": int(data.get("flame", 1)),
        "pir": int(data.get("pir", 0)),
        "current": float(data.get("current", 0)),
    }

    agent = get_agent(device_id)
    result = agent.step(**reading)

    payload = _build_payload(device_id, reading, result)
    save_event(payload)
    broadcast(payload)

    return jsonify({
        "hazard": result["hazard"],
        "level": result["level"],
        "risk_score": result["risk_score"],
        "confidence": result["confidence"],
        "actions": result["actions"],
    })


@app.route("/ingest", methods=["POST"])
def ingest_precomputed():
    """
    Alternative entry point for a payload that already has the FULL
    reasoning result attached (level, hazard, beliefs, actions, ...).
    Useful if reasoning ever moves onto the ESP32 itself. Not used by
    the current Python simulator/backend flow -- see /api/sensor-data
    for that.
    """
    data = request.get_json(force=True, silent=True) or {}

    level = str(data.get("level", "GREEN")).upper()
    hazard = str(data.get("hazard", "SAFE")).upper()
    beliefs = data.get("beliefs") or {"SAFE": 0, "GAS_LEAK": 0, "OVERHEAT": 0, "FIRE": 0}
    actions = data.get("actions") or {}

    payload = {
        "timestamp": data.get("timestamp") or time.time() * 1000,
        "device_id": data.get("device_id", "esp32-01"),
        "level": level,
        "hazard": hazard,
        "confidence": float(data.get("confidence", 0)),
        "risk_score": float(data.get("risk_score", 0)),
        "beliefs": beliefs,
        "rules_fired": data.get("rules_fired", []),
        "actions": actions,
        "reading": data.get("reading", {}),
    }

    save_event(payload)
    broadcast(payload)
    return jsonify({"ok": True}), 200


@app.route("/events")
def events():
    limit = int(request.args.get("limit", 100))
    return jsonify(load_events()[-limit:])


@app.route("/stream")
def stream():
    q: queue.Queue = queue.Queue(maxsize=100)
    with lock:
        subscribers.append(q)

    def gen():
        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"  # heartbeat so proxies don't kill the connection
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=True)
