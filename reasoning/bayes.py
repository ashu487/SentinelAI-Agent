"""
bayes.py  —  SentinelAI reasoning service (v2: dynamic weights)

Same public API as v1:
    agent = SentinelAI()
    result = agent.step(mq2, mq135, temp, hum, flame, pir, current)
"""

import math
from collections import deque

HYPOTHESES = ["SAFE", "GAS_LEAK", "OVERHEAT", "FIRE"]

# Base-rate prior — a lab is safe most of the time. This is a *real* prior now,
# not a uniform one, which is what the doc's "bias toward SAFE" claims.
BASE_PRIOR = {"SAFE": 0.70, "GAS_LEAK": 0.10, "OVERHEAT": 0.10, "FIRE": 0.10}

# ------------------------------------------------------------------
# 1. LIKELIHOODS: continuous, per-hypothesis response curves
# ------------------------------------------------------------------
# Each sensor contributes a *graded* likelihood instead of a coin flip.
# L[h][s](x) returns p(observation | H) for sensor s at normalized value x,
# shaped as a one-sided sigmoid so spikes are monotone (no saturation cliffs).
#
# These are still hand-set, but because they're continuous you can fit them
# later against your calibration provocations (air freshener / lighter /
# resistor load) with a least-squares pass — see `fit_calibration()` stub.

def _sigmoid_curve(mid, steepness, floor):
    """p(x) rises from `floor` toward 1 as x passes `mid`."""
    def f(x):
        return floor + (1.0 - floor) / (1.0 + math.exp(-steepness * (x - mid)))
    return f

# sensor name -> {hypothesis: curve}
LIKELIHOODS = {
    "gas": {
        "SAFE":     _sigmoid_curve(0.55, -9.0, 0.03),  # falls as gas rises
        "GAS_LEAK": _sigmoid_curve(0.35,  9.0, 0.05),
        "OVERHEAT": _sigmoid_curve(0.60,  4.0, 0.10),
        "FIRE":     _sigmoid_curve(0.45,  6.0, 0.15),
    },
    "air": {
        "SAFE":     _sigmoid_curve(0.55, -8.0, 0.05),
        "GAS_LEAK": _sigmoid_curve(0.40,  7.0, 0.08),
        "OVERHEAT": _sigmoid_curve(0.65,  3.5, 0.20),
        "FIRE":     _sigmoid_curve(0.55,  5.0, 0.20),
    },
    "temp": {
        "SAFE":     _sigmoid_curve(0.55, -7.0, 0.03),
        "GAS_LEAK": _sigmoid_curve(0.70,  3.0, 0.10),
        "OVERHEAT": _sigmoid_curve(0.45,  6.0, 0.08),
        "FIRE":     _sigmoid_curve(0.50,  6.0, 0.10),
    },
    "current": {
        "SAFE":     _sigmoid_curve(0.60, -6.0, 0.05),
        "GAS_LEAK": _sigmoid_curve(0.80,  2.0, 0.08),
        "OVERHEAT": _sigmoid_curve(0.40,  6.0, 0.10),
        "FIRE":     _sigmoid_curve(0.65,  3.0, 0.15),
    },
    "flame": {
        "SAFE":     _sigmoid_curve(0.30, -14.0, 0.01),
        "GAS_LEAK": _sigmoid_curve(0.70,  2.0, 0.02),
        "OVERHEAT": _sigmoid_curve(0.75,  1.5, 0.05),
        "FIRE":     _sigmoid_curve(0.30, 12.0, 0.02),
    },
}

# ------------------------------------------------------------------
# 2. FEATURE EXTRACTION (continuous) + SMOOTHING + SLOPE
# ------------------------------------------------------------------
FLAME_BASELINE = 4095
FLAME_DETECT   = 800

def norm(v, lo, hi):
    if hi == lo: return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))

# EWMA per feature — smooths single-sample spikes but preserves real ramps.
EWMA_ALPHA = 0.30

class FeatureExtractor:
    def __init__(self):
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
            "gas":     norm(mq2,    90, 1000),
            "air":     norm(mq135, 500, 3200),
            "temp":    norm(temp,   24,   50),
            "current": norm(current, 0.7, 1.5),
            "flame":   norm(flame, FLAME_BASELINE, FLAME_DETECT),
        }
        f = {k: self._smooth(k, v) for k, v in raw.items()}
        f["gas_slope"] = self.slope["gas"]
        f["temp_slope"] = self.slope["temp"]
        f["human"] = (pir == 1)
        f["humidity"] = hum
        return f

# ------------------------------------------------------------------
# 3. SENSOR RELIABILITY WEIGHTS  (the "dynamic weights" piece)
# ------------------------------------------------------------------
class SensorHealth:
    """
    Tracks whether a sensor is plausible. A sensor's weight drops if it:
      - is pinned at a rail (stuck high/low) for N cycles
      - has near-zero variance over a window while others move (stuck)
    Output: per-sensor weight in (0, 1]. These weights enter both the
    Bayesian likelihood AND the rule engine, so a broken sensor can't
    drive an alarm.
    """
    WINDOW   = 20
    STUCK_RAIL = 0.02

    def __init__(self):
        self.hist = {k: deque(maxlen=self.WINDOW) for k in
                     ("gas", "air", "temp", "current", "flame")}
        self.weight = {k: 1.0 for k in self.hist}

    def update(self, f):
        for k in self.hist:
            self.hist[k].append(f[k])
            w = 1.0
            vals = self.hist[k]
            if len(vals) >= self.WINDOW:
                # stuck at a rail?
                if all(v <= self.STUCK_RAIL or v >= 1 - self.STUCK_RAIL for v in vals):
                    w *= 0.25
                # variance collapse (frozen ADC)?
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / len(vals)
                if var < 1e-5 and k != "flame":  # flame is legitimately flat when clear
                    w *= 0.5
            # EWMA the weight so it recovers once the sensor moves again
            self.weight[k] = 0.85 * self.weight[k] + 0.15 * w
        return self.weight

# ------------------------------------------------------------------
# 4. DYNAMIC-WEIGHTED BAYES UPDATE
# ------------------------------------------------------------------
def weighted_bayes_update(f, prior, weights):
    """
    Weighted naive Bayes:
        log P(H|E) ∝ log P(H) + Σ_s w_s · log L_s(H | x_s)
    A weight of 0 makes that sensor "silent"; a weight of 1 gives full vote.
    Log-space avoids underflow and lets us see per-sensor contributions.
    """
    log_post = {h: math.log(max(prior[h], 1e-12)) for h in HYPOTHESES}
    contrib = {h: {} for h in HYPOTHESES}

    for sensor, table in LIKELIHOODS.items():
        x = f[sensor]
        w = weights.get(sensor, 1.0)
        for h in HYPOTHESES:
            lp = math.log(max(table[h](x), 1e-6))
            log_post[h] += w * lp
            contrib[h][sensor] = round(w * lp, 3)

    # softmax-normalize
    m = max(log_post.values())
    exps = {h: math.exp(v - m) for h, v in log_post.items()}
    Z = sum(exps.values())
    return {h: exps[h] / Z for h in HYPOTHESES}, contrib

# ------------------------------------------------------------------
# 5. CONTEXT-GATED, STRENGTH-SCALED RULES
# ------------------------------------------------------------------
# Each rule returns a *strength* in [0,1] (not a fixed integer), and can
# require context (e.g. human presence, or a corroborating sensor).
# Strength scales the multiplicative evidence, and the reliability weight
# of the sensor the rule depends on gates it — a stuck gas sensor can't
# fire R1 at full force.

def rule_evidence(f, weights):
    ev = {h: 1.0 for h in HYPOTHESES}  # multiplicative evidence, starts at 1
    fired = []

    def boost(h, amount, name):
        if amount > 0.01:
            ev[h] *= (1.0 + amount)
            fired.append(f"{name}({amount:.2f})")

    # R1: gas high — but scaled by how high, gated by gas reliability
    r1 = max(0.0, (f["gas"] - 0.20)) / 0.80 * weights["gas"]
    boost("GAS_LEAK", 3.0 * r1, "R1:gas")

    # R2: bad air *and* gas → strong gas-leak evidence
    r2 = min(f["air"], f["gas"]) * weights["air"] * weights["gas"]
    boost("GAS_LEAK", 2.0 * r2, "R2:air+gas")

    # R3: high temp — require context (current high OR human present),
    #     otherwise a hot day looks like an overheat
    ctx = 1.0 if (f["current"] > 0.35 or f["human"]) else 0.35
    r3 = max(0.0, (f["temp"] - 0.35)) / 0.65 * weights["temp"] * ctx
    boost("OVERHEAT", 3.0 * r3, "R3:temp")

    # R4: overcurrent + heat
    r4 = min(f["current"], f["temp"]) * weights["current"] * weights["temp"]
    boost("OVERHEAT", 2.0 * r4, "R4:I+T")

    # R5: flame + heat → fire
    r5 = f["flame"] * max(0.0, f["temp"] - 0.30) / 0.70 * weights["flame"]
    boost("FIRE", 4.0 * r5, "R5:flame+heat")

    # R6: gas + flame → ignition
    r6 = min(f["gas"], f["flame"]) * weights["gas"] * weights["flame"]
    boost("FIRE", 5.0 * r6, "R6:ignition")

    # R7: human in danger — does NOT boost a hazard, it raises *risk*
    #     (handled in compute_risk) but we log it here for the dashboard.
    if f["human"] and (f["gas"] > 0.30 or f["flame"] > 0.5):
        fired.append("R7:human_at_risk")

    # Temporal boost: a fast-rising hazard is more likely than a slow one.
    # Slope is already EWMA'd, so this is robust to noise.
    if f["gas_slope"] > 0.06:
        boost("GAS_LEAK", 1.5 * weights["gas"], "T1:gas_ramp")
    if f["temp_slope"] > 0.05:
        boost("OVERHEAT", 1.2 * weights["temp"], "T2:heat_ramp")

    return ev, fired

# ------------------------------------------------------------------
# 6. TEMPORAL BAYES FILTER (prior = prev posterior, with forgetting)
# ------------------------------------------------------------------
FORGET = 0.97  # posterior bleeds toward BASE_PRIOR each cycle

def blend_prior(prev_posterior):
    return {h: FORGET * prev_posterior[h] + (1 - FORGET) * BASE_PRIOR[h]
            for h in HYPOTHESES}

# ------------------------------------------------------------------
# 7. RISK SCORING + HYSTERESIS STATE MACHINE
# ------------------------------------------------------------------
RISK_WEIGHTS = {"FIRE": 0.45, "GAS_LEAK": 0.30, "OVERHEAT": 0.20}

def compute_risk(beliefs, f):
    base = sum(RISK_WEIGHTS[h] * beliefs[h] for h in RISK_WEIGHTS)
    base += 0.05 * (1 - beliefs["SAFE"])
    # Severity modulates: how extreme is the *dominant* hazard's evidence?
    dom = max(RISK_WEIGHTS, key=lambda h: beliefs[h])
    severity = {
        "FIRE":     f["flame"],
        "GAS_LEAK": f["gas"],
        "OVERHEAT": max(f["temp"], f["current"]),
    }[dom]
    score = 100 * base * (0.6 + 0.4 * severity)
    # Human presence amplifies urgency (a hazard that can hurt someone)
    if f["human"] and dom != "SAFE":
        score *= 1.25
    return max(0.0, min(100.0, score))

class Hysteresis:
    """Prevents level flapping: enter a higher level at `up`, leave at `down`."""
    THRESH = {"GREEN": 0.0, "YELLOW": 25.0, "RED": 50.0}
    EXIT   = {"YELLOW": 18.0, "RED": 42.0}
    ORDER  = ["GREEN", "YELLOW", "RED"]

    def __init__(self): self.level = "GREEN"

    def update(self, score):
        i = self.ORDER.index(self.level)
        # escalate if score crosses next entry threshold
        while i < 2 and score >= self.THRESH[self.ORDER[i + 1]]:
            i += 1
        # de-escalate only if below the *exit* threshold of current level
        while i > 0 and score < self.EXIT[self.ORDER[i]]:
            i -= 1
        self.level = self.ORDER[i]
        return self.level

# ------------------------------------------------------------------
# 8. ACTION SELECTION (unchanged semantics, still hazard-specific)
# ------------------------------------------------------------------
def decide_actions(hazard, level, f):
    plan = {"exhaust_fan": False, "buzzer": False, "relay_power": True,
            "red_led": False, "yellow_led": False, "green_led": False}

    if level == "GREEN":
        plan["green_led"] = True
        return plan

    if level == "YELLOW":
        plan["yellow_led"] = True
        if hazard in ("GAS_LEAK", "OVERHEAT"):
            plan["exhaust_fan"] = True
        if hazard == "OVERHEAT":
            plan["relay_power"] = False
        if hazard == "FIRE":
            plan["exhaust_fan"] = False
            plan["relay_power"] = False
        return plan

    plan["red_led"] = True
    plan["buzzer"] = True
    if hazard == "GAS_LEAK":
        plan["exhaust_fan"] = True;  plan["relay_power"] = False
    elif hazard == "OVERHEAT":
        plan["exhaust_fan"] = True;  plan["relay_power"] = False
    elif hazard == "FIRE":
        plan["exhaust_fan"] = False; plan["relay_power"] = False  # never ventilate fire
    return plan

# ------------------------------------------------------------------
# 9. AGENT
# ------------------------------------------------------------------
class SentinelAI:
    def __init__(self):
        self.features = FeatureExtractor()
        self.health   = SensorHealth()
        self.hyst     = Hysteresis()
        self.beliefs  = dict(BASE_PRIOR)
        self.history  = []

    def step(self, mq2, mq135, temp, hum, flame, pir, current):
        # Sense
        f = self.features.extract(mq2, mq135, temp, hum, flame, pir, current)
        weights = self.health.update(f)

        # Bayesian update with dynamic weights
        prior = blend_prior(self.beliefs)
        post, contrib = weighted_bayes_update(f, prior, weights)

        # Rule evidence injection (gated by weights + context)
        ev, fired = rule_evidence(f, weights)
        for h in HYPOTHESES:
            post[h] *= ev[h]
        Z = sum(post.values()) or 1.0
        self.beliefs = {h: post[h] / Z for h in HYPOTHESES}

        # Dominant hypothesis
        hazard = max(self.beliefs, key=self.beliefs.get)
        confidence = self.beliefs[hazard]

        # Risk + hysteresis level
        score = compute_risk(self.beliefs, f)
        level = self.hyst.update(score)

        actions = decide_actions(hazard, level, f)

        out = {
            "level": level,
            "hazard": hazard,
            "confidence": round(confidence, 3),
            "risk_score": round(score, 1),
            "beliefs": {k: round(v, 3) for k, v in self.beliefs.items()},
            "rules_fired": fired,
            "sensor_weights": {k: round(v, 2) for k, v in weights.items()},
            "actions": actions,
        }
        self.history.append({**out, "features": f, "contrib": contrib})
        return out
