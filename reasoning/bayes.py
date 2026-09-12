"""
SentinelAI - Intelligent Laboratory Safety & Hazard Response Agent
Python simulation of the ESP32 logic.

Inputs  : MQ-2, MQ-135, DHT22 (temp+hum), Flame, PIR, ACS712
Outputs : Risk level (GREEN/YELLOW/RED), dominant hazard,
          Bayesian beliefs, and actuator action plan.
"""

# ============================================================
# 1. CONFIGURATION & LIKELIHOOD TABLE
# ============================================================

HYPOTHESES = ["SAFE", "GAS_LEAK", "OVERHEAT", "FIRE"]

# Prior beliefs (start uniform)
PRIORS = {"SAFE": 0.25, "GAS_LEAK": 0.25, "OVERHEAT": 0.25, "FIRE": 0.25}

# Likelihood of each feature being "high/true" given a hypothesis
# Feature order: gas_high, air_bad, temp_high, current_high, flame_true
LIKELIHOOD = {
    "SAFE":     {"gas_high": 0.05, "air_bad": 0.10, "temp_high": 0.05,
                 "current_high": 0.05, "flame_true": 0.01},
    "GAS_LEAK": {"gas_high": 0.85, "air_bad": 0.80, "temp_high": 0.20,
                 "current_high": 0.10, "flame_true": 0.05},
    "OVERHEAT": {"gas_high": 0.15, "air_bad": 0.30, "temp_high": 0.80,
                 "current_high": 0.75, "flame_true": 0.20},
    "FIRE":     {"gas_high": 0.70, "air_bad": 0.60, "temp_high": 0.90,
                 "current_high": 0.40, "flame_true": 0.95},
}

# ============================================================
# 2. SENSOR NORMALIZATION
# ============================================================

def norm(value, lo, hi):
    """Clamp and scale a raw reading into 0..1."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def extract_features(mq2, mq135, temp, hum, flame, pir, current):
    """
    Convert raw sensor values into normalized features.
    flame : 0 = flame detected, 1 = no flame (typical digital sensor)
    pir   : 1 = human present,  0 = absent
    """
    gas_level   = norm(mq2,   300, 3000)   # MQ-2
    air_quality = norm(mq135, 400, 3000)   # MQ-135 (higher = worse)
    temp_high = norm(temp, 45, 80)
    current_high= norm(current, 0.5, 3.0)  # ACS712 (Amps)
    flame_true  = (flame == 0)             # active-low flame sensor
    human       = (pir == 1)

    return {
        "gas_level": gas_level,
        "air_quality": air_quality,
        "temp": temp,
        "humidity": hum,
        "temp_high": temp_high,
        "current_high": current_high,
        "flame_true": flame_true,
        "human_present": human,
    }

# ============================================================
# 3. RULE-BASED REASONING
# ============================================================

def apply_rules(f):
    """Forward-chaining rules. Returns evidence dict + fired rule names."""
    evidence = {"GAS_LEAK": 0, "OVERHEAT": 0, "FIRE": 0, "HUMAN": 0}
    fired = []

    if f["gas_level"] > 0.6:
        evidence["GAS_LEAK"] += 3; fired.append("R1:high_gas")

    if f["air_quality"] > 0.7 and f["gas_level"] > 0.4:
        evidence["GAS_LEAK"] += 2; fired.append("R2:poor_air_plus_gas")

    if f["temp"] > 60:
        evidence["OVERHEAT"] += 3; fired.append("R3:high_temp")

    if f["temp"] < 35 and not f["flame_true"]:
        evidence["SAFE"] = evidence.get("SAFE", 0) + 1
        fired.append("R0:ambient_normal")

    if f["current_high"] > 0.6 and f["temp"] > 50:
        evidence["OVERHEAT"] += 2; fired.append("R4:overcurrent_heat")

    if f["flame_true"] and f["temp"] > 70:
        evidence["FIRE"] += 4; fired.append("R5:flame_plus_heat")

    if f["gas_level"] > 0.7 and f["flame_true"]:
        evidence["FIRE"] += 5; fired.append("R6:gas_ignition")

    if f["human_present"] and (f["gas_level"] > 0.5 or f["flame_true"]):
        evidence["HUMAN"] += 2; fired.append("R7:human_at_risk")

    return evidence, fired

# ============================================================
# 4. BAYESIAN BELIEF UPDATE (Naive Bayes)
# ============================================================

def bayesian_update(f, prior):
    posterior = {}
    for h in HYPOTHESES:
        L = LIKELIHOOD[h]
        p = prior[h]
        p *= L["gas_high"]     if f["gas_level"]    > 0.5 else (1 - L["gas_high"])
        p *= L["air_bad"]      if f["air_quality"]  > 0.5 else (1 - L["air_bad"])
        p *= L["temp_high"]    if f["temp_high"]    > 0.5 else (1 - L["temp_high"])
        p *= L["current_high"] if f["current_high"] > 0.5 else (1 - L["current_high"])
        p *= L["flame_true"]   if f["flame_true"]              else (1 - L["flame_true"])
        posterior[h] = p

    total = sum(posterior.values()) or 1.0
    return {h: posterior[h] / total for h in HYPOTHESES}

# ============================================================
# 5. RISK SCORING & LEVEL
# ============================================================

def compute_risk(beliefs):
    """
    Weighted risk score 0..100.
    FIRE weighted highest, SAFE subtracted as a dampener.
    """
    score = 100 * (
        0.40 * beliefs["FIRE"] +
        0.30 * beliefs["GAS_LEAK"] +
        0.20 * beliefs["OVERHEAT"] +
        0.10 * (1 - beliefs["SAFE"])
    )
    return max(0.0, min(100.0, score))


def risk_level(score):
    if score >= 70:
        return "RED"       # CRITICAL
    elif score >= 40:
        return "YELLOW"    # WARNING
    else:
        return "GREEN"     # SAFE

# ============================================================
# 6. DECISION / ACTION SELECTION
# ============================================================

def decide_actions(hazard, level, f):
    """
    Return the actuator states based on hazard + level.
    Key safety insight:
      - FIRE     -> fan OFF (oxygen feeds flames), relay OFF
      - GAS_LEAK -> fan ON  (dilute gas), relay OFF if critical
      - OVERHEAT -> relay OFF (cut power), fan ON
    """
    plan = {
        "exhaust_fan": False,
        "buzzer": False,
        "relay_power": True,   # True = equipment powered
        "red_led": False,
        "yellow_led": False,
        "green_led": False,
    }

    if level == "GREEN":
        plan["green_led"] = True
        return plan

    if level == "YELLOW":
        plan["yellow_led"] = True
        if hazard == "GAS_LEAK":
            plan["exhaust_fan"] = True
        elif hazard == "OVERHEAT":
            plan["exhaust_fan"] = True
            plan["relay_power"] = False
        elif hazard == "FIRE":
            plan["relay_power"] = False
            plan["exhaust_fan"] = False
        return plan

    # RED / CRITICAL
    plan["red_led"] = True
    plan["buzzer"] = True
    if hazard == "GAS_LEAK":
        plan["exhaust_fan"] = True
        plan["relay_power"] = False
    elif hazard == "OVERHEAT":
        plan["exhaust_fan"] = True
        plan["relay_power"] = False
    elif hazard == "FIRE":
        plan["exhaust_fan"] = False   # NEVER ventilate a fire
        plan["relay_power"] = False
    return plan

# ============================================================
# 7. MAIN AGENT PIPELINE
# ============================================================

class SentinelAI:
    def __init__(self):
        self.beliefs = dict(PRIORS)
        self.last_hazard = "SAFE"
        self.confirmation = 0
        self.pending_hazard = None
        self.history = []

    def step(self, mq2, mq135, temp, hum, flame, pir, current):
        # --- Sense + Features ---
        f = extract_features(mq2, mq135, temp, hum, flame, pir, current)

        # --- Rules ---
        evidence, fired = apply_rules(f)

        # --- Bayesian update (uses previous posterior as new prior) ---
        self.beliefs = bayesian_update(f, self.beliefs)

        # Apply rule evidence as multiplicative weight
        for h, e in evidence.items():
            if h in self.beliefs and e > 0:
                self.beliefs[h] *= (1 + 0.15 * e)
        total = sum(self.beliefs.values()) or 1.0
        self.beliefs = {h: self.beliefs[h] / total for h in HYPOTHESES}

        # --- Raw dominant hazard for this single reading ---
        raw_dominant = max(self.beliefs, key=self.beliefs.get)
        confidence = self.beliefs[raw_dominant]

        # --- Temporal confirmation (avoid single-sample flapping) ---
        # FIX: the original version compared raw_dominant against
        # last_hazard="SAFE" before any real reading had been observed,
        # so a brand-new agent's very first .step() always fell back to
        # SAFE regardless of sensor values. Now a new candidate is tracked
        # in pending_hazard and only promoted after 2 consecutive matching
        # readings, so the very first observation is never silently discarded.
        if raw_dominant == self.last_hazard:
            self.pending_hazard = None
            self.confirmation = 0
        elif raw_dominant == self.pending_hazard:
            self.confirmation += 1
        else:
            self.pending_hazard = raw_dominant
            self.confirmation = 1

        if self.pending_hazard is not None and self.confirmation >= 2:
            self.last_hazard = self.pending_hazard
            self.pending_hazard = None
            self.confirmation = 0

        dominant = self.last_hazard

        # --- Risk + level ---
        score = compute_risk(self.beliefs)
        level = risk_level(score)

        # --- Actions ---
        plan = decide_actions(dominant, level, f)

        self.history.append({
            "features": f, "beliefs": dict(self.beliefs),
            "evidence": evidence, "rules": fired,
            "score": score, "level": level, "hazard": dominant,
            "actions": plan
        })
        return {
            "level": level,
            "hazard": dominant,
            "confidence": confidence,
            "risk_score": round(score, 1),
            "beliefs": {k: round(v, 3) for k, v in self.beliefs.items()},
            "rules_fired": fired,
            "actions": plan,
        }

# ============================================================
# 8. PRETTY PRINT
# ============================================================

def print_result(res):
    icons = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}
    print("\n" + "=" * 60)
    print(f"  {icons[res['level']]}  LEVEL   : {res['level']}")
    print(f"  HAZARD  : {res['hazard']}  (confidence {res['confidence']:.2f})")
    print(f"  RISK    : {res['risk_score']} / 100")
    print(f"  BELIEFS : {res['beliefs']}")
    if res["rules_fired"]:
        print(f"  RULES   : {', '.join(res['rules_fired'])}")
    print("  ACTIONS :")
    for k, v in res["actions"].items():
        state = "ON " if v else "OFF"
        print(f"      {k:<12} -> {state}")
    print("=" * 60)

# ============================================================
# 9. INTERACTIVE / DEMO
# ============================================================

def run_interactive():
    agent = SentinelAI()
    print("\nSentinelAI - Interactive Sensor Input")
    print("Enter: MQ2 MQ135 temp humidity flame(0/1) pir(0/1) current(A)")
    print("Example: 1200 500 28 55 1 1 0.4")
    print("Type 'q' to quit.\n")

    while True:
        try:
            raw = input("sensors> ").strip()
            if raw.lower() in ("q", "quit", "exit"):
                break
            vals = raw.split()
            if len(vals) != 7:
                print("Need exactly 7 values.")
                continue
            mq2, mq135, temp, hum, flame, pir, current = map(float, vals)
            res = agent.step(mq2, mq135, temp, hum,
                             int(flame), int(pir), current)
            print_result(res)
        except ValueError:
            print("Invalid input. Use numbers only.")


def run_demo_scenarios():
    print("\n" + "#" * 60)
    print("#  DEMO: 4 scenarios")
    print("#" * 60)

    scenarios = [
        ("Normal lab conditions",
         (1200, 500, 28, 55, 1, 1, 0.4)),

        ("Slow gas leak (MQ-2 rising)",
         (2200, 1500, 30, 60, 1, 0, 0.3)),

        ("Equipment overheating (high current + temp)",
         (1300, 700, 65, 40, 1, 1, 2.5)),

        ("Fire (flame + heat + gas)",
         (2600, 2200, 80, 30, 0, 0, 1.8)),
    ]

    for title, vals in scenarios:
        print(f"\n>>> SCENARIO: {title}")
        agent = SentinelAI()
        res = agent.step(*vals)
        print_result(res)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        run_demo_scenarios()
    else:
        run_demo_scenarios()          # auto-run demo first
        run_interactive()             # then allow manual input
