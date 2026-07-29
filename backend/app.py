import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, render_template

from reasoning.engine import process_reading
from backend.database import init_db, log_event, recent_events

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "static"),
)

# holds latest state per device for the dashboard to poll
_latest_state: dict = {}


@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/api/sensor-data", methods=["POST"])
def ingest_sensor_data():
    """
    This is the endpoint your ESP32 (or the simulator) POSTs to.
    Expected JSON body:
    {
      "device_id": "lab-esp32-1",
      "gas": 250, "smoke": 120, "flame": false,
      "temp": 27.5, "humidity": 55, "motion": false
    }
    Returns the decided actions so the ESP32 knows what to actuate.
    """
    payload = request.get_json(force=True)
    device_id = payload.get("device_id", "unknown-device")
    reading = {
        "gas": payload.get("gas", 0),
        "smoke": payload.get("smoke", 0),
        "flame": bool(payload.get("flame", False)),
        "temp": payload.get("temp", 25),
        "humidity": payload.get("humidity", 50),
        "motion": bool(payload.get("motion", False)),
    }

    result = process_reading(device_id, reading)
    log_event(result)
    _latest_state[device_id] = result

    return jsonify({
        "primary_hazard": result["decision"]["primary_hazard"],
        "risk_score": result["decision"]["risk_score"],
        "risk_band": result["decision"]["risk_band"],
        "actions": result["decision"]["actions"],
    })


@app.route("/api/state")
def get_state():
    """Polled by the dashboard every couple seconds."""
    return jsonify({
        "devices": _latest_state,
        "recent_events": recent_events(30),
    })


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
