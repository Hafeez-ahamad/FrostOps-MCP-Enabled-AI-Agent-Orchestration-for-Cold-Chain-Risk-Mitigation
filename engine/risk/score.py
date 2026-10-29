"""Incident risk score (deterministic, documented formula; no model).

    risk = 100 x severity x duration x exposure x sensitivity      (each factor in [0, 1])

severity     How far past the SOP threshold, taking the worst active rule:
               R-TEMP-01  min(1, excess C / 5)       (5 C past the band max = 1.0)
               R-CONG-01  min(1, (level - 7) / 3)    (congestion 10 = 1.0)
               R-RISK-01  delay probability
               R-ETA-01   1.0                        (breach plus > 1 h delay: worst case)
             floored at 0.2, so any confirmed hit counts.
duration     Minutes the incident has been active / 60, capped at 1, floored at 0.1.
exposure     How much longer the cargo stays exposed: 0.5 + 0.5 x min(1, ETA delay / 120).
             Remaining time to delivery is not in the legacy telemetry, so ETA delay is used
             as the proxy until the TMS mock provides ETA (Phase 3).
sensitivity  Cargo weight from policy/cargo_types.yaml (FRESH 0.7, FROZEN 0.8, PHARMA 1.0;
             configuration assumptions).

The score ranks incidents for the dispatcher. It never decides whether an action runs.
"""

from dataclasses import dataclass

SEVERITY_FLOOR = 0.2
DURATION_FLOOR = 0.1
TEMP_EXCESS_FULL_C = 5.0
CONGESTION_FULL_ABOVE = 3.0  # 7.0 + 3.0 = 10.0
DELAY_FULL_MIN = 120.0


@dataclass(frozen=True)
class RiskInputs:
    temp_excess_c: float | None  # degrees above the band max, if R-TEMP-01 is active
    port_congestion: float | None  # if R-CONG-01 is active
    delay_probability: float | None  # if R-RISK-01 is active
    eta_rule_active: bool  # R-ETA-01
    active_minutes: float
    eta_delay_min: float
    sensitivity: float


@dataclass(frozen=True)
class RiskScore:
    score: float
    severity: float
    duration: float
    exposure: float
    sensitivity: float


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, value))


def severity(inputs: RiskInputs) -> float:
    parts = [0.0]
    if inputs.temp_excess_c is not None:
        parts.append(_clamp(inputs.temp_excess_c / TEMP_EXCESS_FULL_C))
    if inputs.port_congestion is not None:
        parts.append(_clamp((inputs.port_congestion - 7.0) / CONGESTION_FULL_ABOVE))
    if inputs.delay_probability is not None:
        parts.append(_clamp(inputs.delay_probability))
    if inputs.eta_rule_active:
        parts.append(1.0)
    return max(SEVERITY_FLOOR, max(parts))


def risk_score(inputs: RiskInputs) -> RiskScore:
    sev = severity(inputs)
    dur = _clamp(inputs.active_minutes / 60.0, DURATION_FLOOR, 1.0)
    exp = 0.5 + 0.5 * _clamp(inputs.eta_delay_min / DELAY_FULL_MIN)
    sens = _clamp(inputs.sensitivity)
    return RiskScore(
        score=round(100.0 * sev * dur * exp * sens, 1),
        severity=sev,
        duration=dur,
        exposure=exp,
        sensitivity=sens,
    )
