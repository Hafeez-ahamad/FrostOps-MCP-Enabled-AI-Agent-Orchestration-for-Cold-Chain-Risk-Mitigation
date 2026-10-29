"""SOP rules engine (deterministic; no LLM).

Rules come from policy/sop_rules.yaml and cargo bands from policy/cargo_types.yaml. Every
comparison is strict, as the SOP says "exceeds" / "greater than":

    R-TEMP-01  temperature_c > band max for the cargo type      (FRESH: 4.0 C, SOP §1)
    R-CONG-01  port_congestion > 7.0                            (SOP §2; NULL = not port-bound)
    R-RISK-01  risk class == "High Risk" and delay prob > 0.65  (SOP §3)
    R-ETA-01   R-TEMP-01 on the same reading and ETA delay > 60 (SOP §1 mitigation)

Rules run on `valid` and `suspect` readings, never on `invalid` ones. Each hit carries the
reading's data-quality status: only hits on valid readings open incidents; hits on suspect
readings are kept for the monitor's triage.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from engine.cargo import CargoType, load_cargo_types
from engine.dq.checks import parse_ts
from engine.settings import PROJECT_ROOT

SOP_RULES_PATH = PROJECT_ROOT / "policy" / "sop_rules.yaml"
RULE_IDS = ("R-TEMP-01", "R-CONG-01", "R-RISK-01", "R-ETA-01")

DqStatusText = Literal["valid", "suspect", "invalid"]


class RuleDef(BaseModel, frozen=True):
    rule_id: str
    title: str
    sop_clause: str
    sop_quote: str
    sop_threshold: float
    threshold_source: str | None = None
    port_congestion_gt: float | None = None
    risk_classification: str | None = None
    delay_probability_gt: float | None = None
    requires_rule: str | None = None
    eta_delay_min_gt: float | None = None


class RuleBook(BaseModel, frozen=True):
    version: int
    sop_file: str
    rules: dict[str, RuleDef]

    def rule(self, rule_id: str) -> RuleDef:
        return self.rules[rule_id]


def load_rule_book(path: Path = SOP_RULES_PATH) -> RuleBook:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = {rid: RuleDef(rule_id=rid, **spec) for rid, spec in raw["rules"].items()}
    if tuple(rules) != RULE_IDS:
        raise ValueError(f"expected rules {RULE_IDS}, found {tuple(rules)}")
    return RuleBook(version=raw["version"], sop_file=raw["sop_file"], rules=rules)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if number != number else number


@dataclass(frozen=True)
class RuleInput:
    """The fields of one reading that the rules look at (from the legacy columns)."""

    shipment_id: str
    seq: int
    reading_ts: datetime | None
    depot_id: str | None
    cargo_type: str | None
    temperature_c: float | None
    port_congestion: float | None
    risk_classification: str | None
    delay_probability: float | None
    eta_delay_min: float | None
    latitude: float | None = None  # used by the planner, not by the rules
    longitude: float | None = None

    @classmethod
    def from_legacy(cls, row: Mapping[Any, Any]) -> "RuleInput":
        return cls(
            shipment_id=str(row["CNSG_NO"]),
            seq=int(row["PKT_SEQ"]),
            reading_ts=parse_ts(row["EVT_DTTM"]),
            depot_id=row.get("HUB_CD"),
            cargo_type=row.get("PROD_CLS"),
            temperature_c=_num(row.get("RFR_TMP_C")),
            port_congestion=_num(row.get("PORT_Q_IX")),
            risk_classification=row.get("HZD_TIER"),
            delay_probability=_num(row.get("LATE_PCT")),
            eta_delay_min=_num(row.get("ARR_SLIP_MIN")),
            latitude=_num(row.get("GPS_Y")),
            longitude=_num(row.get("GPS_X")),
        )


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    shipment_id: str
    seq: int
    reading_ts: datetime | None
    dq_status: DqStatusText
    sop_clause: str
    sop_ref: str  # e.g. "cold_chain_sop.md §1"
    observed: dict[str, float | str | None] = field(default_factory=dict)
    threshold: dict[str, float | str] = field(default_factory=dict)


class RulesEngine:
    def __init__(
        self,
        book: RuleBook | None = None,
        cargo_types: dict[str, CargoType] | None = None,
    ) -> None:
        self.book = book or load_rule_book()
        self.cargo_types = cargo_types or load_cargo_types()

    def _hit(
        self,
        rule_id: str,
        r: RuleInput,
        dq_status: DqStatusText,
        observed: dict[str, float | str | None],
        threshold: dict[str, float | str],
    ) -> RuleHit:
        rule = self.book.rule(rule_id)
        return RuleHit(
            rule_id=rule_id,
            shipment_id=r.shipment_id,
            seq=r.seq,
            reading_ts=r.reading_ts,
            dq_status=dq_status,
            sop_clause=rule.sop_clause,
            sop_ref=f"{self.book.sop_file} {rule.sop_clause}",
            observed=observed,
            threshold=threshold,
        )

    def evaluate(self, r: RuleInput, dq_status: DqStatusText) -> list[RuleHit]:
        if dq_status == "invalid":
            return []
        hits: list[RuleHit] = []

        band = self.cargo_types.get(r.cargo_type or "")
        temp_hit = False
        if band is not None and r.temperature_c is not None and r.temperature_c > band.max_c:
            temp_hit = True
            hits.append(
                self._hit(
                    "R-TEMP-01",
                    r,
                    dq_status,
                    {"temperature_c": r.temperature_c, "cargo_type": band.code},
                    {"max_c": band.max_c},
                )
            )

        cong = self.book.rule("R-CONG-01")
        assert cong.port_congestion_gt is not None
        if r.port_congestion is not None and r.port_congestion > cong.port_congestion_gt:
            hits.append(
                self._hit(
                    "R-CONG-01",
                    r,
                    dq_status,
                    {"port_congestion": r.port_congestion},
                    {"port_congestion_gt": cong.port_congestion_gt},
                )
            )

        risk = self.book.rule("R-RISK-01")
        assert risk.risk_classification is not None and risk.delay_probability_gt is not None
        if (
            r.risk_classification == risk.risk_classification
            and r.delay_probability is not None
            and r.delay_probability > risk.delay_probability_gt
        ):
            hits.append(
                self._hit(
                    "R-RISK-01",
                    r,
                    dq_status,
                    {
                        "risk_classification": r.risk_classification,
                        "delay_probability": r.delay_probability,
                    },
                    {
                        "risk_classification": risk.risk_classification,
                        "delay_probability_gt": risk.delay_probability_gt,
                    },
                )
            )

        eta = self.book.rule("R-ETA-01")
        assert eta.eta_delay_min_gt is not None
        if temp_hit and r.eta_delay_min is not None and r.eta_delay_min > eta.eta_delay_min_gt:
            hits.append(
                self._hit(
                    "R-ETA-01",
                    r,
                    dq_status,
                    {"eta_delay_min": r.eta_delay_min, "temperature_c": r.temperature_c},
                    {"eta_delay_min_gt": eta.eta_delay_min_gt},
                )
            )
        return hits
