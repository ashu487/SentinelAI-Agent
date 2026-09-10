"""
SentinelAI bridge.
- Receives JSON POSTs from ESP32 at /ingest
- Pushes updates to browsers via SSE at /stream
- Serves index.html and static files
- Persists events to data/events.json
"""

import json, os, time, queue, threading
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ---------- in-memory pub/sub ----------
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
    # keep last 500
    events = events[-500:]
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(events, f, indent=2)
    os.replace(tmp, EVENTS_FILE)

# ---------- routes ----------
@app.route("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.route("/ingest", methods=["POST"])
def ingest():
    data = request.get_json(force=True, silent=True) or {}

    # ---- validate / normalize ----
    level  = str(data.get("level", "GREEN")).upper()
    hazard = str(data.get("hazard", "SAFE")).upper()
    beliefs = data.get("beliefs") or {"SAFE": 0, "GAS_LEAK": 0, "OVERHEAT": 0, "FIRE": 0}
    actions = data.get("actions") or {}

    payload = {
        "timestamp":   data.get("timestamp") or time.time(),
        "device_id":   data.get("device_id", "esp32-01"),
        "level":       level,
        "hazard":      hazard,
        "confidence":  float(data.get("confidence", 0)),
        "risk_score":  float(data.get("risk_score", 0)),
        "beliefs":     beliefs,
        "rules_fired": data.get("rules_fired", []),
        "actions":     actions,
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
        # send a heartbeat comment every 15 s so proxies don't kill the connection
        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ---------- test helper: simulate an ESP32 ----------
@app.route("/simulate", methods=["POST"])
def simulate():
    """POST {"level":"RED","hazard":"FIRE",...} to test the dashboard
       without an ESP32 plugged in."""
    return ingest()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
