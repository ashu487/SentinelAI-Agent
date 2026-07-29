"""
Simulates an ESP32 sending sensor readings over HTTP, so you can build and
test the entire reasoning + dashboard pipeline before hardware is ready.

Run this alongside backend/app.py:
    python backend/app.py        (terminal 1)
    python sensor_simulator.py   (terminal 2)

Then open http://localhost:5000 to watch the dashboard react.

It mostly emits "normal" readings, and periodically injects a scenario
(gas leak, fire, overheat) so you can see the reasoning engine respond.
"""

import random
import time
import requests

API_URL = "http://localhost:5000/api/sensor-data"
DEVICE_ID = "lab-esp32-1"

SCENARIOS = ["normal", "normal", "normal", "gas_leak", "fire", "overheat"]


def normal_reading():
    return {
        "gas": random.uniform(50, 150),
        "smoke": random.uniform(20, 100),
        "flame": False,
        "temp": random.uniform(24, 30),
        "humidity": random.uniform(40, 60),
        "motion": random.choice([True, False]),
    }


def gas_leak_reading(intensity):
    return {
        "gas": 400 + intensity * 800,
        "smoke": 100 + intensity * 300,
        "flame": False,
        "temp": random.uniform(25, 32),
        "humidity": random.uniform(40, 60),
        "motion": False,
    }


def fire_reading(intensity):
    return {
        "gas": random.uniform(100, 300),
        "smoke": 200 + intensity * 600,
        "flame": intensity > 0.5,
        "temp": 35 + intensity * 40,
        "humidity": random.uniform(20, 40),
        "motion": False,
    }


def overheat_reading(intensity):
    return {
        "gas": random.uniform(50, 150),
        "smoke": random.uniform(20, 100),
        "flame": False,
        "temp": 40 + intensity * 25,
        "humidity": random.uniform(30, 50),
        "motion": False,
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
        print(f"sent={reading} -> hazard={result['primary_hazard']} "
              f"risk={result['risk_score']} band={result['risk_band']} actions={result['actions']}")
    except requests.RequestException as e:
        print(f"failed to reach backend: {e}")


if __name__ == "__main__":
    print("SentinelAI sensor simulator running. Ctrl+C to stop.")
    while True:
        scenario = random.choice(SCENARIOS)
        run_scenario(scenario, steps=6)
        time.sleep(3)
