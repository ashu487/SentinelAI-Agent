"""
Bayesian fusion layer.

We treat each hazard as a hypothesis H in {Fire, GasLeak, Overheat, Normal}
and each sensor reading as evidence E. We discretize raw readings into
{low, medium, high} bins, then combine per-sensor likelihoods with Bayes'
theorem:

    P(H | E1, E2, E3) proportional to P(H) * P(E1|H) * P(E2|H) * P(E3|H)

(naive-Bayes style conditional independence assumption between sensors
given the hypothesis -- standard simplification, mention this explicitly
in your report as a stated assumption).

The likelihood tables below are hand-authored priors based on domain
reasoning. For a stronger AI report, log real sensor data during testing
and re-estimate these tables from frequency counts (that upgrades this
from "hand-tuned" to "learned from data").
"""

HAZARDS = ["fire", "gas_leak", "overheat", "normal"]

# Prior belief before seeing any evidence (mostly nothing is wrong)
PRIORS = {
    "fire": 0.02,
    "gas_leak": 0.03,
    "overheat": 0.03,
    "normal": 0.92,
}

# P(evidence_level | hazard) for each sensor, per hazard hypothesis.
# levels: low, medium, high
LIKELIHOODS = {
    "gas": {
        "fire":     {"low": 0.6, "medium": 0.3, "high": 0.1},
        "gas_leak": {"low": 0.05, "medium": 0.25, "high": 0.70},
        "overheat": {"low": 0.8, "medium": 0.15, "high": 0.05},
        "normal":   {"low": 0.95, "medium": 0.045, "high": 0.005},
    },
    "smoke": {
        "fire":     {"low": 0.05, "medium": 0.25, "high": 0.70},
        "gas_leak": {"low": 0.6, "medium": 0.3, "high": 0.1},
        "overheat": {"low": 0.5, "medium": 0.35, "high": 0.15},
        "normal":   {"low": 0.96, "medium": 0.035, "high": 0.005},
    },
    "temp": {
        "fire":     {"low": 0.05, "medium": 0.25, "high": 0.70},
        "gas_leak": {"low": 0.75, "medium": 0.2, "high": 0.05},
        "overheat": {"low": 0.05, "medium": 0.25, "high": 0.70},
        "normal":   {"low": 0.9, "medium": 0.09, "high": 0.01},
    },
    "flame": {
        # binary sensor treated as its own "high confidence" evidence
        "fire":     {"low": 0.02, "high": 0.98},
        "gas_leak": {"low": 0.95, "high": 0.05},
        "overheat": {"low": 0.97, "high": 0.03},
        "normal":   {"low": 0.995, "high": 0.005},
    },
}


def discretize(value: float, low_th: float, high_th: float) -> str:
    if value >= high_th:
        return "high"
    if value >= low_th:
        return "medium"
    return "low"


def bayesian_posterior(evidence_levels: dict) -> dict:
    """
    evidence_levels: {'gas': 'high', 'smoke': 'medium', 'temp': 'low', 'flame': 'low'}
    returns normalized posterior probability per hazard, e.g.
        {'fire': 0.71, 'gas_leak': 0.18, 'overheat': 0.05, 'normal': 0.06}
    """
    unnormalized = {}
    for hazard in HAZARDS:
        p = PRIORS[hazard]
        for sensor, level in evidence_levels.items():
            table = LIKELIHOODS.get(sensor, {}).get(hazard, {})
            p *= table.get(level, 1e-6)
        unnormalized[hazard] = p

    total = sum(unnormalized.values()) or 1e-9
    return {h: round(v / total, 4) for h, v in unnormalized.items()}
