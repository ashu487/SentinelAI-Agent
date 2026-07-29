"""
Rule-based reasoning layer.

Each function inspects raw sensor values and returns a dict of boolean
flags. These flags feed both the Bayesian fusion layer (as discretized
evidence) and act as hard safety overrides for the decision layer.

Thresholds below are starting points -- you WILL need to recalibrate
these once you test with real sensors (MQ-2 in particular is noisy
and needs a warm-up + baseline calibration in clean air).
"""

THRESHOLDS = {
    "gas_ppm_high": 1000,      # MQ-2 analog->ppm approx, recalibrate
    "gas_ppm_medium": 400,
    "smoke_high": 700,         # raw analog 0-4095 (ESP32 ADC) or 0-1023
    "smoke_medium": 300,
    "temp_high_c": 55,
    "temp_medium_c": 40,
    "temp_rate_c_per_min": 5,  # rapid rise = overheating signal
    "flame_detected": True,    # digital flame sensor, active LOW or HIGH depending on module
}


def evaluate_rules(reading: dict, temp_history: list) -> dict:
    """
    reading: {
        'gas': float, 'smoke': float, 'flame': bool,
        'temp': float, 'humidity': float, 'motion': bool
    }
    temp_history: list of recent (timestamp, temp) tuples for rate-of-change calc
    """
    flags = {
        "gas_leak_suspected": False,
        "fire_suspected": False,
        "overheating_suspected": False,
        "critical_override": False,  # bypasses risk scoring, acts immediately
    }

    gas = reading.get("gas", 0)
    smoke = reading.get("smoke", 0)
    flame = reading.get("flame", False)
    temp = reading.get("temp", 25)

    # --- Gas leak rule ---
    if gas >= THRESHOLDS["gas_ppm_high"]:
        flags["gas_leak_suspected"] = True
    if gas >= THRESHOLDS["gas_ppm_high"] and smoke >= THRESHOLDS["smoke_medium"]:
        flags["critical_override"] = True  # gas + smoke together = don't wait

    # --- Fire rule ---
    if flame:
        flags["fire_suspected"] = True
        flags["critical_override"] = True  # flame sensor is high-confidence, act now
    if smoke >= THRESHOLDS["smoke_high"] and temp >= THRESHOLDS["temp_medium_c"]:
        flags["fire_suspected"] = True

    # --- Overheating rule (rate of change) ---
    rate = _temp_rate(temp_history)
    if temp >= THRESHOLDS["temp_high_c"] or rate >= THRESHOLDS["temp_rate_c_per_min"]:
        flags["overheating_suspected"] = True

    return flags


def _temp_rate(temp_history: list) -> float:
    """Approximate °C/min rise using the oldest and newest sample in the window."""
    if len(temp_history) < 2:
        return 0.0
    (t0, v0), (t1, v1) = temp_history[0], temp_history[-1]
    minutes = max((t1 - t0) / 60.0, 1e-6)
    return (v1 - v0) / minutes
