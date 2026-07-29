"""
Orchestrates the full perceive -> reason -> decide pipeline for one
incoming sensor reading. This is the module you'll most want to walk
your faculty through -- it's the "agent brain."
"""

import time

from .rules import evaluate_rules, THRESHOLDS
from .bayesian import discretize, bayesian_posterior
from .decision import decide

# rolling temperature history per device, used for rate-of-change rule
_temp_history: dict[str, list] = {}
_HISTORY_WINDOW_SEC = 120


def _update_temp_history(device_id: str, temp: float):
    now = time.time()
    hist = _temp_history.setdefault(device_id, [])
    hist.append((now, temp))
    cutoff = now - _HISTORY_WINDOW_SEC
    _temp_history[device_id] = [(t, v) for t, v in hist if t >= cutoff]
    return _temp_history[device_id]


def process_reading(device_id: str, reading: dict) -> dict:
    """
    reading: {'gas': float, 'smoke': float, 'flame': bool, 'temp': float,
              'humidity': float, 'motion': bool}

    returns a full result dict combining rules + bayesian posterior + decision,
    ready to log and to send back to the ESP32 / show on the dashboard.
    """
    temp_hist = _update_temp_history(device_id, reading.get("temp", 25))

    rule_flags = evaluate_rules(reading, temp_hist)

    evidence_levels = {
        "gas": discretize(reading.get("gas", 0),
                           THRESHOLDS["gas_ppm_medium"], THRESHOLDS["gas_ppm_high"]),
        "smoke": discretize(reading.get("smoke", 0),
                             THRESHOLDS["smoke_medium"], THRESHOLDS["smoke_high"]),
        "temp": discretize(reading.get("temp", 25),
                            THRESHOLDS["temp_medium_c"], THRESHOLDS["temp_high_c"]),
        "flame": "high" if reading.get("flame") else "low",
    }

    posteriors = bayesian_posterior(evidence_levels)
    decision = decide(posteriors, rule_flags)

    return {
        "device_id": device_id,
        "timestamp": time.time(),
        "reading": reading,
        "evidence_levels": evidence_levels,
        "rule_flags": rule_flags,
        "posteriors": posteriors,
        "decision": decision,
    }
