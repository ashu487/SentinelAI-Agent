"""
SentinelAI - Intelligent Laboratory Safety & Hazard Response Agent
Python simulation of the ESP32 logic.

Inputs  : MQ-2, MQ-135, DHT22 (temp+hum), Flame, PIR, ACS712
Outputs : Risk level (GREEN/YELLOW/RED), dominant hazard,
          Bayesian beliefs, and actuator action plan.

--------------------------------------------------------------------
CALIBRATION NOTE (this revision)
--------------------------------------------------------------------
The original norm() bounds (mq2: 300-3000, mq135: 400-3000, current:
0.5-3.0) were guesses that didn't match this rig's real readings.
From the live mqtt_bridge.py log:

    quiet baseline : mq2 ~70-130,  mq135 ~490-850,  current ~0.66-0.67
    observed spike : mq2 ~980-1150, mq135 ~3100-3140

A real ~10x mq2 spike (982) only normalized to 0.25 under the old
bounds -- below every trigger threshold -- so GAS_LEAK never fired.
All norm() windows below are rescaled to this hardware's actual
baseline/spike range, and every rule / Bayesian / risk threshold that
depended on the old scale has been lowered to match. Re-tune the
temp/current/flame bounds once you have real overheat/fire spike
readings the same way mq2/mq135 were tuned here.
--------------------------------------------------------------------
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

# Threshold used everywhere a feature is checked as "high" for the
# Bayesian update. Lowered from 0.5 -> 0.35 so the rescaled features
# below cross it at realistic spike levels instead of needing an
# almost-saturated reading.
BAYES_HIGH_THRESHOLD = 0.35

# ============================================================
# 2. SENSOR NORMALIZATION
# ============================================================

def norm(value, lo, hi):
    """Clamp and scale a raw reading into 0..1."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


# Flame sensor calibration (ESP32 ADC range is typically 0-4095).
# Still active-low even in analog mode: reading is HIGH with no flame and
# DROPS as a flame gets closer/stronger.
# TODO calibrate these two with your hardware teammate:
#   FLAME_BASELINE -- raw reading in a clear room right now (~4000-4095,
#                      matches the steady 4095 seen in the live log)
#   FLAME_DETECT   -- raw reading with an actual small flame held close
#                      to the sensor (test this once you can do it safely)
FLAME_BASELINE = 4095
FLAME_DETECT = 800


def extract_features(mq2, mq135, temp, hum, flame, pir, current):
    """
    Convert raw sensor values into normalized features.
    flame : raw analog ADC reading (0-4095 typical). HIGH (~FLAME_BASELINE)
            = no flame, LOW (~FLAME_DETECT) = flame detected/close.
    pir   : 1 = human present,  0 = absent
    """
    # --- Rescaled to this rig's real quiet-baseline / spike readings ---
    gas_level    = norm(mq2,    90,  1000)   # MQ-2   (was 300-3000)
    air_quality  = norm(mq135, 500,  3200)   # MQ-135 (was 400-3000)
    temp_high    = norm(temp,   24,    50)   # DHT22  (was 30-70; room ~23-24C)
    current_high = norm(current, 0.7, 1.5)  # ACS712 (was 0.5-3.0; idle ~0.66A)
    # norm() with lo > hi still interpolates correctly: a raw value at
    # FLAME_BASELINE gives 0, at FLAME_DETECT gives 1, values in between
    # scale linearly regardless of which threshold is numerically larger.
    flame_level  = norm(flame, FLAME_BASELINE, FLAME_DETECT)  # 0=clear, 1=strong flame
    flame_true   = flame_level > 0.5
    human        = (pir == 1)

    return {
        "gas_level": gas_level,
        "air_quality": air_quality,
        "temp": temp,
        "humidity": hum,
        "temp_high": temp_high,
        "current_high": current_high,
        "flame_level": flame_level,
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

    # Thresholds lowered across the board to match the rescaled features
    # above -- e.g. mq2=982 now normalizes to ~0.90, well past 0.20.
    if f["gas_level"] > 0.20:                                   # was 0.30
        evidence["GAS_LEAK"] += 3; fired.append("R1:high_gas")

    if f["air_quality"] > 0.5 and f["gas_level"] > 0.25:        # was 0.7 / 0.4
        evidence["GAS_LEAK"] += 2; fired.append("R2:poor_air_plus_gas")

    if f["temp"] > 40:                                          # was 60
        evidence["OVERHEAT"] += 3; fired.append("R3:high_temp")

    if f["current_high"] > 0.4 and f["temp"] > 35:              # was 0.6 / 50
        evidence["OVERHEAT"] += 2; fired.append("R4:overcurrent_heat")

    if f["flame_true"] and f["temp"] > 45:                      # was 70
        evidence["FIRE"] += 4; fired.append("R5:flame_plus_heat")

    if f["gas_level"] > 0.5 and f["flame_true"]:                # was 0.7
        evidence["FIRE"] += 5; fired.append("R6:gas_ignition")

    if f["human_present"] and (f["gas_level"] > 0.3 or f["flame_true"]):  # was 0.5
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
        p *= L["gas_high"]     if f["gas_level"]    > BAYES_HIGH_THRESHOLD else (1 - L["gas_high"])
        p *= L["air_bad"]      if f["air_quality"]  > BAYES_HIGH_THRESHOLD else (1 - L["air_bad"])
        p *= L["temp_high"]    if f["temp_high"]    > BAYES_HIGH_THRESHOLD else (1 - L["temp_high"])
        p *= L["current_high"] if f["current_high"] > BAYES_HIGH_THRESHOLD else (1 - L["current_high"])
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
    # Lowered from 70/40 so a real spike (which now produces a more
    # moderate belief shift under the rescaled features) still crosses
    # into YELLOW/RED instead of staying GREEN.
    if score >= 50:
        return "RED"       # CRITICAL   (was 70)
    elif score >= 25:
        return "YELLOW"    # WARNING    (was 40)
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

        # --- Immediate hazard selection ---
        # Use the current sensor reading immediately; no 2-reading delay.
        #
        # BUGFIX: this used to unconditionally force dominant = "GAS_LEAK"
        # whenever f["gas_level"] > 0.20, regardless of what the weighted
        # Bayesian posterior actually said. That meant a real fire (which
        # often also trips the gas sensor) or an overheat event would still
        # be reported and *acted on* as a gas leak -- e.g. decide_actions()
        # would turn the exhaust fan ON during an actual fire, which is
        # exactly the "never ventilate a fire" rule this file is trying to
        # enforce elsewhere. The rule-evidence weighting a few lines above
        # already pushes GAS_LEAK's belief up when gas is high, via
        # apply_rules()/R1/R2, so the argmax below is trusted as-is instead
        # of being overridden.
        dominant = raw_dominant

        self.last_hazard = dominant

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
    icons = {"GREEN": "\U0001F7E2", "YELLOW": "\U0001F7E1", "RED": "\U0001F534"}
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
    print("#  DEMO: recalibrated to real log values")
    print("#" * 60)

    scenarios = [
        ("Normal lab conditions (quiet baseline from log)",
         (95, 528, 23.8, 98, 4095, 1, 0.668)),

        ("MQ-2/MQ-135 spike exactly as seen in mqtt_bridge.py log",
         (982, 3139, 24.1, 98, 4095, 1, 0.664)),

        ("Equipment overheating (high current + temp)",
         (300, 700, 45, 40, 4095, 1, 1.8)),

        ("Fire (flame + heat + gas)",
         (900, 2200, 60, 30, 800, 0, 1.2)),
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
        run_interactive()             # wait for live sensor input