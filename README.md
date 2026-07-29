# SentinelAI — software starter

This is a working starter for the SentinelAI reasoning engine + dashboard.
It runs entirely on your laptop with a **simulated sensor feed**, so you
don't need any hardware to start developing and testing the AI logic.
When your hardware teammate's ESP32 is ready, it just becomes another
client POSTing to the same `/api/sensor-data` endpoint the simulator uses.

## Project structure

```
sentinelai/
  reasoning/
    rules.py       - rule-based reasoning (thresholds, hard overrides)
    bayesian.py     - Bayesian fusion (posterior risk per hazard)
    decision.py      - risk score -> action mapping
    engine.py        - orchestrates rules + bayesian + decision
  backend/
    app.py           - Flask API (ingest sensor data, serve dashboard)
    database.py       - SQLite event logging
  dashboard/
    templates/index.html
    static/style.css, script.js
  sensor_simulator.py - fakes an ESP32 sending readings over HTTP
  requirements.txt
```

## Step 1 — install dependencies

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Step 2 — run the backend

```bash
python backend/app.py
```

This starts the Flask server on http://localhost:5000 and creates
`backend/sentinelai.db` (SQLite) to log every event.

## Step 3 — run the simulator (separate terminal)

```bash
python sensor_simulator.py
```

This sends fake sensor readings every second, occasionally injecting a
gas leak / fire / overheating scenario, and prints the engine's decision
for each reading.

## Step 4 — open the dashboard

Go to http://localhost:5000 in your browser. You'll see live risk cards
per device and a table of recent events, auto-refreshing every 2 seconds.

## How the reasoning pipeline works

1. **`rules.py`** — fast, explainable thresholds (e.g. flame sensor
   triggers `critical_override` immediately, no waiting on probability).
2. **`bayesian.py`** — discretizes each sensor reading into low/medium/high
   and combines per-sensor likelihoods with Bayes' theorem to get a
   posterior probability for each hypothesis: fire, gas_leak, overheat,
   normal.
3. **`decision.py`** — maps the highest-probability hazard + its risk band
   (low/medium/high/critical) to a list of actions (fan, buzzer, power
   cutoff, notifications), with rule-based overrides able to force
   `critical` regardless of the Bayesian score.
4. **`engine.py`** — glues it together per reading and also tracks a
   rolling temperature history per device to detect rate-of-change
   (fast temperature rise = overheating signal even before it crosses
   the absolute threshold).

## What to do next (in order)

1. **Run steps 1–4 above right now** and watch the dashboard react to
   simulated gas leak / fire / overheat scenarios. Confirm you understand
   what each module is doing — you'll need to explain this in your report.
2. **Tune the numbers.** `rules.THRESHOLDS` and `bayesian.LIKELIHOODS` are
   starting guesses. Once your hardware teammate gives you real sensor
   readings (both normal-room baseline and induced-hazard test readings,
   e.g. holding a lighter near the flame sensor), update these tables so
   they reflect your actual sensors' behavior.
3. **Add a `/api/action-ack` style flow later** if you want the ESP32 to
   confirm it executed an action (nice-to-have for your report's
   reliability section, not required).
4. **Once the ESP32 firmware is ready**, point it at
   `POST http://<your-laptop-ip>:5000/api/sensor-data` with the same JSON
   shape the simulator uses:
   ```json
   {"device_id": "lab-esp32-1", "gas": 250, "smoke": 120,
    "flame": false, "temp": 27.5, "humidity": 55, "motion": false}
   ```
   The response body tells the ESP32 exactly which actions to execute
   (`exhaust_fan_on`, `buzzer_on`, `power_cutoff`, etc.) — your hardware
   teammate just needs to map those strings to GPIO/relay pins.
5. **For the AI report**, log a batch of test events (normal + each
   induced hazard scenario) from the SQLite DB and compute basic
   evaluation metrics: false positive rate, false negative rate,
   detection latency. This is exactly the kind of evaluation section
   that scores well.

## Notes / things to be upfront about in your report

- The Bayesian fusion assumes conditional independence between sensors
  given the hazard hypothesis (a standard naive-Bayes simplification) —
  state this explicitly rather than presenting it as a full joint model.
- `LIKELIHOODS` in `bayesian.py` are hand-authored priors. For extra
  credit, log real sensor data during testing and re-estimate these
  tables from observed frequencies — that upgrades the story from
  "hand-tuned" to "learned from data."
