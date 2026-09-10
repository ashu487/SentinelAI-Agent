"""
Simulates an ESP32 sending sensor readings over HTTP, so you can build and
test the entire reasoning + dashboard pipeline before hardware is ready.

Run alongside backend/app.py:
    python backend/app.py        (terminal 1)
    python sensor_simulator.py   (terminal 2)

Then open http://localhost:5000 to watch the dashboard react live (SSE).

flame: 0 = flame detected (active-low sensor), 1 = no flame
pir:   1 = human present, 0 = absent

Each scenario is sent for several consecutive steps because SentinelAI
requires 2 consecutive matching readings before it confirms a hazard
change (temporal smoothing to avoid single-sample noise flapping).
"""

import random
import time
import requests

API_URL = "http://localhost:5000/api/sensor-data"
DEVICE_ID = "lab-esp32-1"

SCENARIOS = ["normal", "normal", "normal", "gas_leak", "fire", "overheat"]


def normal_reading():
    return {
        "mq2": random.uniform(200, 900),
        "mq135": random.uniform(200, 700),
        "temp": random.uniform(24, 30),
        "hum": random.uniform(40, 60),
        "flame": 1,
        "pir": random.choice([0, 1]),
        "current": random.uniform(0.1, 0.4),
    }


def gas_leak_reading(intensity):
    return {
        "mq2": 900 + intensity * 2200,
        "mq135": 700 + intensity * 1800,
        "temp": random.uniform(28, 34),
        "hum": random.uniform(40, 60),
        "flame": 1,
        "pir": 0,
        "current": random.uniform(0.1, 0.4),
    }


def fire_reading(intensity):
    return {
        "mq2": 1500 + intensity * 1500,
        "mq135": 1200 + intensity * 1200,
        "temp": 40 + intensity * 45,
        "hum": random.uniform(20, 40),
        "flame": 0 if intensity > 0.5 else 1,
        "pir": 0,
        "current": random.uniform(0.5, 1.5),
    }


def overheat_reading(intensity):
    return {
        "mq2": random.uniform(300, 900),
        "mq135": random.uniform(300, 900),
        "temp": 45 + intensity * 30,
        "hum": random.uniform(30, 50),
        "flame": 1,
        "pir": random.choice([0, 1]),
        "current": 1.0 + intensity * 2.0,
    }


def run_scenario(name, steps=8):
    print(f"\n--- injecting scenario: {name} ---")
    for i in range(steps):
        intensity = i / max(steps - 1, 1)
        if name == "gas_leak":
            reading = gas_leak_reading(intensity)
        elif name == "fire":
            reading = fire_reading(intensity)
        elif name == "overheat":
            reading = overheat_reading(intensity)
        else:
            reading = normal_reading()

        send(reading)
        time.sleep(1)


def send(reading):
    payload = {"device_id": DEVICE_ID, **reading}
    try:
        r = requests.post(API_URL, json=payload, timeout=3)
        r.raise_for_status()
        result = r.json()
        print(f"sent={reading} -> hazard={result['hazard']} "
              f"level={result['level']} risk={result['risk_score']} actions={result['actions']}")
    except requests.RequestException as e:
        print(f"failed to reach backend: {e}")


if __name__ == "__main__":
    print("SentinelAI sensor simulator running. Ctrl+C to stop.")
    while True:
        scenario = random.choice(SCENARIOS)
        run_scenario(scenario, steps=6)
        time.sleep(3)
