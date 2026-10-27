"""
bayes.py — SentinelAI reasoning engine (v4: condition-dependent dynamic priors + weights)

Pipeline, once per telemetry reading:
  1. Calibrate once per room (60 s of quiet telemetry) -> baseline + deadband per channel.
  2. Normalise each sensor against THIS room's baseline (0 = quiet, 1 = full hazard).
  3. Read the lab condition (occupied? load on?).
  4. Dynamic prior     : P(H | condition) instead of one static prior.
  5. Dynamic weights   : per-sensor weight = reliability (stuck/frozen check)
                         x trust for the current condition.
  6. Weighted naive-Bayes update (log space) + context-gated rule evidence.
  7. Risk score -> hysteresis level -> hazard-specific actuator plan.

Public API:
    cal = Calibrator()
    cal.add(raw_sample)                  # raw sample dict, see Calibrator.add
    profile = cal.finish()               # then save_profile(profile, ...)

    agent  = SentinelAI()                # loads baseline.json
    agent  = SentinelAI(profile=profile) # or pass an in-memory profile

    result = agent.step(mq2, mq135, temp, hum, flame_raw, pir, current)
"""

import json
import math
import time
from collections import deque
from pathlib import Path

HYPOTHESES = ["SAFE", "GAS_LEAK", "OVERHEAT", "FIRE"]
FEATURE_KEYS = ("gas", "air", "temp", "current", "flame")
BASELINE_FILE = Path(__file__).resolve().parent.parent / "baseline.json"
PROFILE_VERSION = 4

# ============================================================
# 1. CONSTANTS
# ============================================================

# Prior before any lab condition is applied.
BASE_PRIOR = {"SAFE": 0.70, "GAS_LEAK": 0.10, "OVERHEAT": 0.10, "FIRE": 0.10}

# DYNAMIC PRIOR — multipliers on BASE_PRIOR chosen by the lab condition.
# Tune these from logged data; they are starting values, not measured.
OCCUPIED_MULT = {
    True:  {"SAFE": 1.0, "GAS_LEAK": 1.5, "OVERHEAT": 1.0, "FIRE": 1.3},  # people: solvents, hot plates
    False: {"SAFE": 1.5, "GAS_LEAK": 0.8, "OVERHEAT": 1.0, "FIRE": 0.8},  # empty room: quiet is likely
}
LOAD_MULT = {
    True:  {"SAFE": 0.8, "GAS_LEAK": 1.0, "OVERHEAT": 2.0, "FIRE": 1.2},  # equipment drawing current
    False: {"SAFE": 1.0, "GAS_LEAK": 1.0, "OVERHEAT": 1.0, "FIRE": 1.0},
}
LOAD_ON_DELTA_A = 0.40  # current above calibrated baseline by this much = load is on

# DYNAMIC WEIGHTS — extra trust per sensor when a condition holds.
TRUST_MULT = {
    "occupied": {"gas": 1.2, "flame": 1.2},     # exposure matters more with people present
    "load_on":  {"current": 1.3, "temp": 1.1},  # current separates OVERHEAT from FIRE
}
MAX_WEIGHT = 1.5

# Full-hazard scale: how far above the room's quiet baseline counts as "1.0".
HAZARD_SPAN = {"mq2": 800.0, "mq135": 2000.0, "temp": 12.0, "current": 1.5}
# Minimum deadband: changes smaller than this never register as evidence.
MIN_DEADBAND = {"mq2": 40.0, "mq135": 80.0, "temp": 2.0, "current": 0.20}
PROFILE_KEYS = ("mq2", "mq135", "temp", "current")

FLAME_CLEAR  = 4095   # active-low analog flame sensor, clear room
FLAME_DETECT = 800    # flame close to sensor

EWMA_ALPHA = 0.30
FORGET     = 0.97     # how fast the belief returns to the dynamic prior

# ============================================================
# 2. LIKELIHOOD CURVES (per sensor, per hypothesis, over 0..1)
# ============================================================

def _sigmoid(mid, steepness, floor):
    """p(x) rises from `floor` toward 1 as x passes `mid`."""
    def f(x):
        return floor + (1.0 - floor) / (1.0 + math.exp(-steepness * (x - mid)))
    return f


LIKELIHOODS = {
    "gas": {
        "SAFE":     _sigmoid(0.55, -9.0, 0.03),
        "GAS_LEAK": _sigmoid(0.35,  9.0, 0.05),
        "OVERHEAT": _sigmoid(0.60,  4.0, 0.10),
        "FIRE":     _sigmoid(0.45,  6.0, 0.15),
    },
    "air": {
        "SAFE":     _sigmoid(0.55, -8.0, 0.05),
        "GAS_LEAK": _sigmoid(0.40,  7.0, 0.08),
        "OVERHEAT": _sigmoid(0.65,  3.5, 0.20),
        "FIRE":     _sigmoid(0.55,  5.0, 0.20),
    },
    "temp": {
        "SAFE":     _sigmoid(0.55, -7.0, 0.03),
        "GAS_LEAK": _sigmoid(0.70,  3.0, 0.10),
        "OVERHEAT": _sigmoid(0.45,  6.0, 0.08),
        "FIRE":     _sigmoid(0.50,  6.0, 0.10),
    },
    "current": {
        "SAFE":     _sigmoid(0.60, -6.0, 0.05),
        "GAS_LEAK": _sigmoid(0.80,  2.0, 0.08),
        "OVERHEAT": _sigmoid(0.40,  6.0, 0.10),
        "FIRE":     _sigmoid(0.65,  3.0, 0.15),
    },
    "flame": {
        "SAFE":     _sigmoid(0.30, -14.0, 0.01),
        "GAS_LEAK": _sigmoid(0.70,   2.0, 0.02),
        "OVERHEAT": _sigmoid(0.75,   1.5, 0.05),
        "FIRE":     _sigmoid(0.30,  12.0, 0.02),
    },
}

# ============================================================
# 3. CALIBRATION (per room, 60 s of quiet telemetry)
# ============================================================

class Calibrator:
    """Collects quiet samples and derives a per-channel baseline + deadband."""

    # Flame is active-low: a small ADC value means a flame is being seen.
    # Only that rejects a sample at add() time. Fixed absolute ranges for the
    # other sensors were removed because they depend on the hardware and room.
    FLAME_ACTIVE_BELOW = 2500
    MIN_SAMPLES = 10

    def __init__(self):
        self.samples = []
        self.rejected = 0
        self.reasons = {}      # why samples were rejected, e.g. {"flame": 12}

    def add(self, raw):
        """raw = dict with mq2, mq135, temp, hum, flame (raw ADC), pir, current."""
        if raw["flame"] < self.FLAME_ACTIVE_BELOW:
            self.rejected += 1
            self.reasons["flame"] = self.reasons.get("flame", 0) + 1
            return False
        self.samples.append(raw)
        return True

    def finish(self):
        """Robust baseline: drop outliers per channel (median +/- 4 robust sigma)."""
        if len(self.samples) < self.MIN_SAMPLES:
            raise RuntimeError(
                f"only {len(self.samples)} usable samples (need {self.MIN_SAMPLES}); "
                f"reasons={self.reasons}")
        keep = list(self.samples)
        for k in PROFILE_KEYS:
            vals = sorted(s[k] for s in keep)
            med = vals[len(vals) // 2]
            mad = sorted(abs(v - med) for v in vals)[len(vals) // 2]
            tol = max(4 * 1.4826 * mad, MIN_DEADBAND[k])
            before = len(keep)
            keep = [s for s in keep if abs(s[k] - med) <= tol]
            if len(keep) < before:
                self.reasons[f"outlier_{k}"] = self.reasons.get(f"outlier_{k}", 0) + (before - len(keep))
        if len(keep) < self.MIN_SAMPLES:
            raise RuntimeError(
                f"only {len(keep)} consistent samples after outlier removal; "
                f"reasons={self.reasons}")
        self.kept = len(keep)

        profile = {}
        for k in PROFILE_KEYS:
            v = sorted(s[k] for s in keep)
            n = len(v)
            p5 = v[int(0.05 * (n - 1))]
            p95 = v[int(0.95 * (n - 1))]
            profile[k] = {
                "base": v[n // 2],                                   # median = quiet level
                "deadband": max(p95 - p5, MIN_DEADBAND[k]),          # quiet noise width
            }
        return profile


def save_profile(profile, path=BASELINE_FILE, n_samples=0, rejected=0):
    Path(path).write_text(json.dumps({
        "version": PROFILE_VERSION,
        "captured_at": time.time(),
        "n_samples": n_samples,
        "n_rejected": rejected,
        "profile": profile,
    }, indent=2))


def load_profile(path=BASELINE_FILE):
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return None
    if data.get("version") != PROFILE_VERSION:
        return None          # old format -> recalibrate
    return data["profile"]

# ============================================================
# 4. FEATURE EXTRACTION (normalise + EWMA + slope)
# ============================================================

def _clamp01(x):
    return max(0.0, min(1.0, x))


def normalise(value, key, profile):
    p = profile[key]
    return _clamp01((value - p["base"] - p["deadband"]) / HAZARD_SPAN[key])


def normalise_flame(raw):
    return _clamp01((FLAME_CLEAR - raw) / (FLAME_CLEAR - FLAME_DETECT))


class FeatureExtractor:
    def __init__(self, profile):
        self.profile = profile
        self.smoothed = {}
        self.prev = {}
        self.slope = {}

    def _smooth(self, key, raw):
        if key not in self.smoothed:
            self.smoothed[key] = raw
        else:
            self.smoothed[key] = EWMA_ALPHA * raw + (1 - EWMA_ALPHA) * self.smoothed[key]
        prev = self.prev.get(key, self.smoothed[key])
        self.slope[key] = self.smoothed[key] - prev
        self.prev[key] = self.smoothed[key]
        return self.smoothed[key]

    def extract(self, mq2, mq135, temp, hum, flame, pir, current):
        raw = {
            "gas":     normalise(mq2,     "mq2",    self.profile),
            "air":     normalise(mq135,   "mq135",  self.profile),
            "temp":    normalise(temp,    "temp",   self.profile),
            "current": normalise(current, "current", self.profile),
            "flame":   normalise_flame(flame),
        }
        f = {k: self._smooth(k, v) for k, v in raw.items()}
        f["gas_slope"] = self.slope["gas"]
        f["temp_slope"] = self.slope["temp"]
        f["human"] = (pir == 1)
        f["load_on"] = (current - self.profile["current"]["base"]) > LOAD_ON_DELTA_A
        f["humidity"] = hum
        return f

# ============================================================
# 5. SENSOR HEALTH (reliability part of the dynamic weights)
# ============================================================

class SensorHealth:
    """
    Reliability in (0, 1] for the two MQ gas sensors:
      - pinned at an ADC rail for a whole window -> open/short wiring (x0.25)
      - completely frozen (no ADC noise at all)   -> stuck reading    (x0.5)
    Weights recover smoothly once the sensor moves again.
    Flame, current and temperature are not checked (legitimately flat at times).
    """
    WINDOW = 20

    def __init__(self):
        self.hist = {"gas": deque(maxlen=self.WINDOW), "air": deque(maxlen=self.WINDOW)}
        self.weight = {k: 1.0 for k in FEATURE_KEYS}

    def update(self, raw_mq2, raw_mq135):
        for key, raw in (("gas", raw_mq2), ("air", raw_mq135)):
            h = self.hist[key]
            h.append(raw)
            target = 1.0
            if len(h) == self.WINDOW:
                if all(v <= 0 or v >= 4095 for v in h):
                    target *= 0.25
                mean = sum(h) / len(h)
                var = sum((v - mean) ** 2 for v in h) / len(h)
                if var < 1.0:
                    target *= 0.5
            self.weight[key] = 0.85 * self.weight[key] + 0.15 * target
        return dict(self.weight)


def condition_weights(health, occupied, load_on):
    """Dynamic weight = reliability x condition trust, capped at MAX_WEIGHT."""
    w = dict(health)
    for cond, active in (("occupied", occupied), ("load_on", load_on)):
        if active:
            for sensor, mult in TRUST_MULT[cond].items():
                w[sensor] *= mult
    return {k: min(MAX_WEIGHT, v) for k, v in w.items()}

# ============================================================
# 6. DYNAMIC PRIOR
# ============================================================

def dynamic_prior(occupied, load_on):
    """P(H | lab condition): the base rates shift with what the lab is doing."""
    raw = {h: BASE_PRIOR[h] * OCCUPIED_MULT[occupied][h] * LOAD_MULT[load_on][h]
           for h in HYPOTHESES}
    Z = sum(raw.values())
    return {h: raw[h] / Z for h in HYPOTHESES}

# ============================================================
# 7. WEIGHTED BAYES UPDATE (naive Bayes, log space)
# ============================================================

def weighted_bayes_update(f, prior, weights):
    log_post = {h: math.log(max(prior[h], 1e-12)) for h in HYPOTHESES}
    for sensor, table in LIKELIHOODS.items():
        x = f[sensor]
        w = weights[sensor]
        for h in HYPOTHESES:
            log_post[h] += w * math.log(max(table[h](x), 1e-6))
    m = max(log_post.values())
    exps = {h: math.exp(v - m) for h, v in log_post.items()}
    Z = sum(exps.values())
    return {h: exps[h] / Z for h in HYPOTHESES}

# ============================================================
# 8. CONTEXT-GATED RULE EVIDENCE
# ============================================================

def rule_evidence(f, weights):
    ev = {h: 1.0 for h in HYPOTHESES}
    fired = []

    def boost(h, amount, name):
        if amount > 0.01:
            ev[h] *= (1.0 + amount)
            fired.append(f"{name}({amount:.2f})")

    r1 = max(0.0, f["gas"] - 0.20) / 0.80 * weights["gas"]
    boost("GAS_LEAK", 3.0 * r1, "R1:gas")

    r2 = min(f["air"], f["gas"]) * weights["air"] * weights["gas"]
    boost("GAS_LEAK", 2.0 * r2, "R2:air+gas")

    # Heat only counts as overheat if a load is on or a person is present.
    ctx = 1.0 if (f["load_on"] or f["human"]) else 0.35
    r3 = max(0.0, f["temp"] - 0.35) / 0.65 * weights["temp"] * ctx
    boost("OVERHEAT", 3.0 * r3, "R3:temp")

    r4 = min(f["current"], f["temp"]) * weights["current"] * weights["temp"]
    boost("OVERHEAT", 2.0 * r4, "R4:I+T")

    r5 = f["flame"] * max(0.0, f["temp"] - 0.30) / 0.70 * weights["flame"]
    boost("FIRE", 4.0 * r5, "R5:flame+heat")

    r6 = min(f["gas"], f["flame"]) * weights["gas"] * weights["flame"]
    boost("FIRE", 5.0 * r6, "R6:ignition")

    if f["human"] and (f["gas"] > 0.30 or f["flame"] > 0.5):
        fired.append("R7:human_at_risk")

    if f["gas_slope"] > 0.06:
        boost("GAS_LEAK", 1.5 * weights["gas"], "T1:gas_ramp")
    if f["temp_slope"] > 0.05:
        boost("OVERHEAT", 1.2 * weights["temp"], "T2:heat_ramp")

    return ev, fired

# ============================================================
# 9. RISK + HYSTERESIS
# ============================================================

# Relative danger of each hazard. FIRE is the reference (1.0), so a
# fully-certain fire at full severity scores 100. Gas and overheat are
# scored against it, so each can still reach RED when certain.
# (v3 used weights 0.45/0.30/0.20 with no rescale, so a 95%-certain fire
#  scored ~48 = YELLOW unless a person was present.)
RISK_WEIGHTS = {"FIRE": 1.0, "GAS_LEAK": 0.7, "OVERHEAT": 0.55}


def compute_risk(beliefs, f):
    base = sum(RISK_WEIGHTS[h] * beliefs[h] for h in RISK_WEIGHTS)
    base /= max(RISK_WEIGHTS.values())                      # 0..1
    dom = max(RISK_WEIGHTS, key=lambda h: beliefs[h])
    severity = {"FIRE": f["flame"], "GAS_LEAK": f["gas"],
                "OVERHEAT": max(f["temp"], f["current"])}[dom]
    score = 100 * base * (0.6 + 0.4 * severity)
    if f["human"] and dom != "SAFE":
        score *= 1.25
    return max(0.0, min(100.0, score))


class Hysteresis:
    THRESH = {"GREEN": 0.0, "YELLOW": 25.0, "RED": 50.0}
    EXIT   = {"YELLOW": 18.0, "RED": 42.0}
    ORDER  = ["GREEN", "YELLOW", "RED"]

    def __init__(self):
        self.level = "GREEN"

    def update(self, score):
        i = self.ORDER.index(self.level)
        while i < 2 and score >= self.THRESH[self.ORDER[i + 1]]:
            i += 1
        while i > 0 and score < self.EXIT[self.ORDER[i]]:
            i -= 1
        self.level = self.ORDER[i]
        return self.level

# ============================================================
# 10. ACTION PLAN
# ============================================================

def decide_actions(hazard, level):
    plan = {"exhaust_fan": False, "buzzer": False, "relay_power": True,
            "sprinkler": False, "red_led": False, "yellow_led": False, "green_led": False}

    if level == "GREEN":
        plan["green_led"] = True
        return plan

    if level == "YELLOW":
        plan["yellow_led"] = True
        if hazard in ("GAS_LEAK", "OVERHEAT"):
            plan["exhaust_fan"] = True
        if hazard in ("OVERHEAT", "FIRE"):
            plan["relay_power"] = False
        return plan

    plan["red_led"] = True
    plan["buzzer"] = True
    if hazard in ("GAS_LEAK", "OVERHEAT"):
        plan["exhaust_fan"] = True
        plan["relay_power"] = False
    elif hazard == "FIRE":
        plan["sprinkler"] = True          # never ventilate a fire
        plan["relay_power"] = False
    return plan

# ============================================================
# 11. AGENT
# ============================================================

class SentinelAI:
    def __init__(self, profile=None, baseline_path=BASELINE_FILE):
        if profile is None:
            profile = load_profile(baseline_path)
        if profile is None:
            raise FileNotFoundError(
                "No valid baseline. Run the bridge once in a quiet room to calibrate.")
        self.profile = profile
        self.features = FeatureExtractor(profile)
        self.health = SensorHealth()
        self.hyst = Hysteresis()
        self.beliefs = dict(BASE_PRIOR)

    def step(self, mq2, mq135, temp, hum, flame, pir, current):
        f = self.features.extract(mq2, mq135, temp, hum, flame, pir, current)
        health = self.health.update(mq2, mq135)
        weights = condition_weights(health, f["human"], f["load_on"])

        # Dynamic prior for THIS condition, blended with last belief.
        cond_prior = dynamic_prior(f["human"], f["load_on"])
        prior = {h: FORGET * self.beliefs[h] + (1 - FORGET) * cond_prior[h]
                 for h in HYPOTHESES}

        post = weighted_bayes_update(f, prior, weights)
        ev, fired = rule_evidence(f, weights)
        for h in HYPOTHESES:
            post[h] *= ev[h]
        Z = sum(post.values()) or 1.0
        self.beliefs = {h: post[h] / Z for h in HYPOTHESES}

        hazard = max(self.beliefs, key=self.beliefs.get)
        score = compute_risk(self.beliefs, f)
        level = self.hyst.update(score)

        return {
            "level":          level,
            "hazard":         hazard,
            "confidence":     round(self.beliefs[hazard], 3),
            "risk_score":     round(score, 1),
            "beliefs":        {k: round(v, 3) for k, v in self.beliefs.items()},
            "prior":          {k: round(v, 3) for k, v in cond_prior.items()},
            "condition":      {"occupied": f["human"], "load_on": f["load_on"]},
            "rules_fired":    fired,
            "sensor_weights": {k: round(v, 2) for k, v in weights.items()},
            "actions":        decide_actions(hazard, level),
        }
