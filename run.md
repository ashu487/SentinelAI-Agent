# Running SentinelAI — Complete Setup Guide

Your project now splits into three machines doing three different jobs:

```
┌────────────────────────┐      ┌──────────────────┐      ┌─────────────────────┐
│  Hardware Device       │      │   Broker         │      │  Your Laptop        │
│  (ESP32)               │      │   (Mosquitto)    │      │  (Dashboard + AI)   │
│                        │      │                  │      │                     │
│  reads sensors         │ MQTT │  10.87.61.232    │ WS   │  sentinel_bridge.py │
│  drives actuators      │─────►│  1883 + 9001     │─────►│  index.html         │
│  POSTs to bridge       │      │                  │      │                     │
└────────────────────────┘      └──────────────────┘      └─────────────────────┘
                                        ▲
                                        │
                              reasoning/bayes.py
                              runs on the laptop
```

- **Broker** — a small server (Mosquitto) that forwards messages. Runs on a machine always on the network. In your config it's at `10.87.61.232`.
- **Your laptop** — runs the AI reasoning (`bayes.py`) and displays the dashboard.
- **Hardware device (ESP32)** — reads sensors, sends raw readings to the laptop, receives actuator commands back.

## Your Laptop (dashboard + AI)

This is the machine that runs `sentinel_bridge.py` and displays the dashboard.

### 1. Install Python dependencies

From the **project root** (`sentinelai/`):

```bash
cd sentinelai
python -m venv venv

# Linux / macOS
source venv/bin/activate
# Windows cmd
venv\Scripts\activate
# Windows PowerShell
.\venv\Scripts\Activate.ps1

pip install paho-mqtt
```

That's the only external dependency. `reasoning.bayes` uses only the standard library.

### 2. Verify the package structure

```bash
python -c "from reasoning.bayes import SentinelAI; print('OK')"
```

Must print `OK`. If it fails, you're missing `reasoning/__init__.py` or running from the wrong directory.

### 3. Configure the bridge

Open `backend/sentinel_bridge.py` and confirm these match your broker:

```python
BROKER_HOST = "10.87.61.232"    # the broker's IP
BROKER_PORT = 1883              # plain MQTT port
MQTT_USER   = "sentinel"
MQTT_PASS   = "87654321"
DEVICE_ID   = "sentinel"
```

If your broker is on the same laptop for testing, use `"127.0.0.1"`.

### 4. Run the bridge

From the project root:

```bash
python -m backend.sentinel_bridge
```

Expected output:

```
[bridge] connected to 10.87.61.232:1883
[bridge] listening on sentinel/sentinel/cmd
[bridge] simulator started — publishing to sentinel/sentinel/telemetry

[bridge] → GREEN  SAFE      risk= 12.3  fan=False buzzer=False
[bridge] → YELLOW GAS_LEAK  risk= 48.1  fan=True  buzzer=False
[bridge] → RED    FIRE      risk= 88.4  fan=False buzzer=True
```

Leave it running.

### 5. Serve the dashboard

Open a **second terminal**:

```bash
cd sentinelai/dashboard/templates
python -m http.server 8000
```

You'll see:

```
Serving HTTP on 0.0.0.0 port 8000 ...
```

### 6. Open the dashboard

In a browser:

```
http://localhost:8000/index.html
```

Within ~1 second:
- The connection pill turns green (`● live`)
- The level badge cycles GREEN → YELLOW → RED as the simulator runs
- Belief bars shift, rules chips appear, event table grows

If the pill stays red, check the browser console (F12). The usual culprit is that the browser can't reach `ws://10.87.61.232:9001`.

### 7. Reach the dashboard from another device on the same network

If you want to open the dashboard on your phone or a second laptop:

```bash
# find your laptop's IP
ip addr                # Linux
ifconfig               # macOS
ipconfig               # Windows
```

Say your laptop is `192.168.1.42`. On the other device, open:

```
http://192.168.1.42:8000/index.html
```

The dashboard will connect to the same broker over WebSocket and show the same data. Multiple viewers work simultaneously.

---


### One critical change on the laptop side

Your current `sentinel_bridge.py` runs an **internal simulator**. To make it listen to the ESP32 instead, replace the `run_simulator()` call with a subscription to `sentinel/sentinel/sensors`.

Add this to the bridge:

```python
TOPIC_SENSORS = f"sentinel/{DEVICE_ID}/sensors"

def on_connect(c, userdata, flags, rc):
    if rc == 0:
        c.subscribe(TOPIC_COMMAND)
        c.subscribe(TOPIC_SENSORS)        # ← NEW
        print(f"[bridge] listening on {TOPIC_COMMAND} and {TOPIC_SENSORS}")

def on_message(c, userdata, msg):
    # existing manual-override logic
    if msg.topic == TOPIC_COMMAND:
        try:
            data = json.loads(msg.payload.decode())
            for k in manual:
                if k in data:
                    manual[k] = bool(data[k])
            print(f"[bridge] manual override: {manual}")
        except Exception as e:
            print(f"[bridge] bad cmd JSON: {e}")
        return

    # NEW: raw sensor reading from the ESP32
    if msg.topic == TOPIC_SENSORS:
        try:
            reading = json.loads(msg.payload.decode())
        except Exception as e:
            print(f"[bridge] bad sensor JSON: {e}")
            return

        # default any missing fields so agent.step() never crashes
        reading.setdefault("mq2", 0)
        reading.setdefault("mq135", 0)
        reading.setdefault("temp", 25)
        reading.setdefault("hum", 50)
        reading.setdefault("flame", 1)
        reading.setdefault("pir", 0)
        reading.setdefault("current", 0)

        # run the agent
        result = agent.step(**reading)
        publish(reading, result)
```

And change `__main__`:

```python
if __name__ == "__main__":
    from reasoning.bayes import SentinelAI
    agent = SentinelAI()             # persistent, needed for temporal smoothing
    start_mqtt()
    try:
        while True:
            time.sleep(1)            # keep the main thread alive
    except KeyboardInterrupt:
        client.loop_stop()
        client.disconnect()
```

Now the flow is:

```
ESP32 → sentinel/sentinel/sensors → bridge → agent.step() → sentinel/sentinel/telemetry → browser
                                                            ↘ also published for ESP32 to read → ESP32 drives actuators
```

The ESP32 both sends raw data AND receives the actions on the same loop.

---

## Part 4 — Full Startup Sequence

### On the broker machine

```bash
mosquitto -c /etc/mosquitto/conf.d/sentinel.conf
```

### On the laptop

Terminal 1:
```bash
cd sentinelai
source venv/bin/activate
python -m backend.sentinel_bridge
```

Terminal 2:
```bash
cd sentinelai/dashboard/templates
python -m http.server 8000
```

Browser:
```
http://localhost:8000/index.html
```

### What you should see

| Where | What |
|-------|------|
| Broker log | connections from ESP32 and bridge |
| Laptop terminal | `[bridge] → GREEN SAFE risk=12.3 ...` every 2 s |
| ESP32 serial | `sent: {...}` then `actuators updated: ...` |
| Browser | live card, beliefs, sensors panel, event table |
roves the entire software stack works. Then when the hardware arrives, replace `run_simulator()` with the `on_message` handler for `sensors` topic.

Order of operations:
1. Get broker running.
2. Run bridge with simulator → dashboard shows GREEN/YELLOW/RED cycling.
3. Once that's stable, plug in ESP32 and switch the bridge to accept real sensor data.
4. Sanity-check one actuator (buzzer) on the ESP32 side before wiring the rest.

Don't try to debug hardware and software simultaneously. Bring them up independently and only then join them.
