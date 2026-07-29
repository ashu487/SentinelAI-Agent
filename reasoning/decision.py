"""
Decision layer: turns (risk scores + rule flags) into concrete actions.

Framed as a simple decision policy: escalate action strength as
probability of hazard rises, with rule-based critical_override
short-circuiting straight to the most aggressive response (don't wait
on probability when a flame sensor or combined gas+smoke event fires).
"""

RISK_BANDS = [
    (0.0, 0.25, "low"),
    (0.25, 0.5, "medium"),
    (0.5, 0.75, "high"),
    (0.75, 1.01, "critical"),
]

# action matrix: hazard -> risk band -> list of actions
ACTION_MATRIX = {
    "gas_leak": {
        "low": ["log_only"],
        "medium": ["exhaust_fan_on", "notify_app"],
        "high": ["exhaust_fan_on", "buzzer_on", "notify_app"],
        "critical": ["exhaust_fan_on", "buzzer_on", "power_cutoff", "notify_app", "notify_sms"],
    },
    "fire": {
        "low": ["log_only"],
        "medium": ["buzzer_on", "notify_app"],
        "high": ["buzzer_on", "power_cutoff", "notify_app"],
        "critical": ["buzzer_on", "power_cutoff", "notify_app", "notify_sms"],
    },
    "overheat": {
        "low": ["log_only"],
        "medium": ["exhaust_fan_on", "notify_app"],
        "high": ["exhaust_fan_on", "power_cutoff", "notify_app"],
        "critical": ["exhaust_fan_on", "power_cutoff", "notify_app", "notify_sms"],
    },
}


def band_for(score: float) -> str:
    for lo, hi, name in RISK_BANDS:
        if lo <= score < hi:
            return name
    return "critical"


def decide(posteriors: dict, rule_flags: dict) -> dict:
    """
    posteriors: bayesian posterior per hazard, e.g. {'fire': 0.7, ...}
    rule_flags: output of rules.evaluate_rules()

    returns: {
        'primary_hazard': 'fire',
        'risk_score': 0.71,
        'risk_band': 'high',
        'actions': ['buzzer_on', 'power_cutoff', 'notify_app']
    }
    """
    hazards = {h: p for h, p in posteriors.items() if h != "normal"}
    primary_hazard = max(hazards, key=hazards.get)
    score = hazards[primary_hazard]
    band = band_for(score)

    if rule_flags.get("critical_override"):
        band = "critical"
        score = max(score, 0.9)

    actions = ACTION_MATRIX.get(primary_hazard, {}).get(band, ["log_only"])

    return {
        "primary_hazard": primary_hazard,
        "risk_score": round(score, 3),
        "risk_band": band,
        "actions": actions,
    }
