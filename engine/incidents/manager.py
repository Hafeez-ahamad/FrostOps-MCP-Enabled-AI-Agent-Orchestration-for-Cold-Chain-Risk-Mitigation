"""Incident open / merge / clear logic (deterministic; no LLM).

- Only rule hits on `valid` readings change incidents. Hits on `suspect` readings are kept in
  `suspect_hits` for the monitor's triage; they never open an incident on their own.
- A shipment has at most one open incident. A new valid hit on a shipment with an open
  incident is merged into it (rules seen, hit count, latest evidence, risk score).
- An open incident is cleared once the shipment has sent valid readings with no rule hits for
  `clear_after_min` minutes since its last hit. A later hit opens a new incident.
- An incident is marked `needs_attention` when one of its actions fails after its retries;
  it stays the shipment's open incident (later hits still merge into it).
- Incident ids are deterministic (`INC-<shipment>-<n>`), so a replay produces the same ids.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from engine.cargo import CargoType, load_cargo_types
from engine.risk.score import RiskInputs, RiskScore, risk_score
from engine.rules.engine import DqStatusText, RuleHit, RuleInput

CLEAR_AFTER_MIN = 30.0

IncidentStatus = Literal["open", "cleared", "needs_attention"]
EventKind = Literal["opened", "merged", "cleared", "needs_attention"]


@dataclass(frozen=True)
class Incident:
    incident_id: str
    shipment_id: str
    depot_id: str | None
    cargo_type: str | None
    status: IncidentStatus
    opened_at: datetime
    last_hit_at: datetime
    rule_ids: tuple[str, ...]  # every rule seen, in first-seen order
    hit_count: int
    first_seq: int
    last_seq: int
    risk: RiskScore
    latest_evidence: tuple[RuleHit, ...]  # hits from the latest hit reading
    cleared_at: datetime | None = None


@dataclass(frozen=True)
class IncidentEvent:
    kind: EventKind
    at: datetime
    incident: Incident


def _risk_inputs(
    hits: list[RuleHit], r: RuleInput, band: CargoType | None, minutes: float
) -> RiskInputs:
    by_rule = {h.rule_id: h for h in hits}
    temp_excess = None
    if "R-TEMP-01" in by_rule and band is not None and r.temperature_c is not None:
        temp_excess = r.temperature_c - band.max_c
    return RiskInputs(
        temp_excess_c=temp_excess,
        port_congestion=r.port_congestion if "R-CONG-01" in by_rule else None,
        delay_probability=r.delay_probability if "R-RISK-01" in by_rule else None,
        eta_rule_active="R-ETA-01" in by_rule,
        active_minutes=minutes,
        eta_delay_min=r.eta_delay_min or 0.0,
        sensitivity=band.sensitivity if band is not None else 1.0,
    )


@dataclass
class IncidentManager:
    cargo_types: dict[str, CargoType] = field(default_factory=load_cargo_types)
    clear_after_min: float = CLEAR_AFTER_MIN
    incidents: dict[str, Incident] = field(default_factory=dict)  # by incident id
    suspect_hits: list[RuleHit] = field(default_factory=list)
    _open: dict[str, str] = field(default_factory=dict)  # shipment id -> open incident id
    _counter: dict[str, int] = field(default_factory=dict)  # shipment id -> incidents so far

    def open_incident_for(self, shipment_id: str) -> Incident | None:
        incident_id = self._open.get(shipment_id)
        return self.incidents[incident_id] if incident_id else None

    def process(
        self, r: RuleInput, dq_status: DqStatusText, hits: list[RuleHit]
    ) -> IncidentEvent | None:
        if dq_status != "valid":
            self.suspect_hits.extend(h for h in hits if h.dq_status == "suspect")
            return None
        if r.reading_ts is None:
            return None
        now = r.reading_ts
        current = self.open_incident_for(r.shipment_id)
        band = self.cargo_types.get(r.cargo_type or "")

        if not hits:
            if current and now - current.last_hit_at >= timedelta(minutes=self.clear_after_min):
                cleared = replace(current, status="cleared", cleared_at=now)
                self.incidents[cleared.incident_id] = cleared
                del self._open[r.shipment_id]
                return IncidentEvent("cleared", now, cleared)
            return None

        rule_ids = tuple(dict.fromkeys(h.rule_id for h in hits))
        if current is None:
            n = self._counter.get(r.shipment_id, 0) + 1
            self._counter[r.shipment_id] = n
            incident = Incident(
                incident_id=f"INC-{r.shipment_id}-{n:02d}",
                shipment_id=r.shipment_id,
                depot_id=r.depot_id,
                cargo_type=r.cargo_type,
                status="open",
                opened_at=now,
                last_hit_at=now,
                rule_ids=rule_ids,
                hit_count=1,
                first_seq=r.seq,
                last_seq=r.seq,
                risk=risk_score(_risk_inputs(hits, r, band, 0.0)),
                latest_evidence=tuple(hits),
            )
            self._open[r.shipment_id] = incident.incident_id
            self.incidents[incident.incident_id] = incident
            return IncidentEvent("opened", now, incident)

        minutes = (now - current.opened_at).total_seconds() / 60
        merged = replace(
            current,
            last_hit_at=now,
            rule_ids=tuple(dict.fromkeys(current.rule_ids + rule_ids)),
            hit_count=current.hit_count + 1,
            last_seq=r.seq,
            risk=risk_score(_risk_inputs(hits, r, band, minutes)),
            latest_evidence=tuple(hits),
        )
        self.incidents[merged.incident_id] = merged
        return IncidentEvent("merged", now, merged)

    def mark_needs_attention(self, incident_id: str, at: datetime) -> IncidentEvent | None:
        incident = self.incidents.get(incident_id)
        if incident is None or incident.status == "cleared":
            return None
        flagged = replace(incident, status="needs_attention")
        self.incidents[incident_id] = flagged
        return IncidentEvent("needs_attention", at, flagged)

    def ranked_open(self) -> list[Incident]:
        """Open incidents, highest risk first (ties: oldest first)."""
        open_incidents = [self.incidents[i] for i in self._open.values()]
        return sorted(open_incidents, key=lambda i: (-i.risk.score, i.opened_at, i.incident_id))
