"""Deterministic planner: candidate generation, costing and selection (no LLM).

Candidates come from the incident's active rules (spec section 8.3). Each gets a utility:

    utility = harm avoided - delay cost - irreversibility cost

Harm avoided (illustrative weights, chosen so the ranking follows the SOP):
    divert_to_cold_storage     110 with R-ETA-01 (breach + >1 h delay, SOP §1), else 30
    divert_to_overflow_depot   100 with R-CONG-01 (SOP §2)
    notify_driver_restart_...   60 with R-TEMP-01 (SOP §1)
    escalate_tier2_manager      50 with R-RISK-01 (SOP §3), else 40 as the fallback for a
                                temperature breach nobody can fix remotely (10 after a
                                door-open triage: a recovered door needs no manager)
Delay cost: 0.2 per minute of road travel to the diversion site (road km = 1.3 x straight
line, 60 km/h). Irreversibility cost: 10 for "until departure", 5 for "no", 0 for "yes".

Temperature incidents also get `create_incident_ticket` (L2), both as a companion action and
as a candidate (harm 10, or 30 when the monitor confidently triages a door-open event, which
also lowers notifying the driver to 15). If the top two candidates are within
CLOSE_CALL_MARGIN, the plan is a close call: in C2 that is when the LLM planner is consulted;
C0 takes the top candidate.
Facility status comes from the facilities service; if it is unavailable, no diversion is
proposed and the plan is marked low confidence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from engine.actions.models import Evidence, Proposal
from engine.actions.policy import ActionCatalog, Reversibility, load_action_catalog
from engine.geo import haversine_km
from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.rules.engine import RuleInput

CLOSE_CALL_MARGIN = 10.0
DELAY_COST_PER_MIN = 0.2
ROAD_FACTOR = 1.3
SPEED_KMH = 60.0
IRREVERSIBILITY_COST = {
    Reversibility.YES: 0.0,
    Reversibility.NO: 5.0,
    Reversibility.UNTIL_DEPARTURE: 10.0,
}
TICKET = "create_incident_ticket"


@dataclass(frozen=True)
class Candidate:
    action: str
    target: str
    harm_avoided: float
    delay_cost: float
    irreversibility_cost: float
    reason: str

    @property
    def utility(self) -> float:
        return round(self.harm_avoided - self.delay_cost - self.irreversibility_cost, 2)

    def describe(self) -> str:
        return f"{self.action}->{self.target} (utility {self.utility:.1f})"


@dataclass(frozen=True)
class Plan:
    incident_id: str
    candidates: tuple[Candidate, ...]  # ranked, best first
    companions: tuple[Candidate, ...] = ()
    close_call: bool = False
    confidence: Literal["normal", "low"] = "normal"
    notes: tuple[str, ...] = field(default=())

    @property
    def primary(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def travel_minutes(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    return haversine_km(lat, lon, to_lat, to_lon) * ROAD_FACTOR / SPEED_KMH * 60


class DeterministicPlanner:
    def __init__(
        self, catalog: ActionCatalog | None = None, margin: float = CLOSE_CALL_MARGIN
    ) -> None:
        self.catalog = catalog or load_action_catalog()
        self.margin = margin

    def _irreversibility(self, action: str) -> float:
        policy = self.catalog.get(action)
        return IRREVERSIBILITY_COST[policy.reversible] if policy else 0.0

    def _candidate(
        self, action: str, target: str, harm: float, delay: float, reason: str
    ) -> Candidate:
        return Candidate(
            action, target, harm, round(delay, 2), self._irreversibility(action), reason
        )

    def plan(
        self,
        incident: Incident,
        reading: RuleInput,
        facilities: list[FacilityStatus] | None,
        triage: str | None = None,
    ) -> Plan:
        """`triage` is the monitor's verdict (C1/C2 only). A confident "door_open_event" makes
        recording the excursion the best candidate instead of contacting the driver."""
        rules = set(incident.rule_ids)
        door_open = triage == "door_open_event"
        temp, eta = "R-TEMP-01" in rules, "R-ETA-01" in rules
        candidates: list[Candidate] = []
        notes: list[str] = []
        confidence: Literal["normal", "low"] = "normal"
        position = (reading.latitude, reading.longitude)
        have_position = None not in position

        if facilities is None:
            confidence = "low"
            notes.append("facilities service unavailable: no diversion proposed")
        if not have_position:
            notes.append("no position in the latest reading: no diversion proposed")
        usable = facilities if facilities is not None and have_position else []
        lat, lon = reading.latitude or 0.0, reading.longitude or 0.0

        if temp:
            candidates.append(
                self._candidate(
                    "notify_driver_restart_cooling",
                    incident.shipment_id,
                    15.0 if door_open else 60.0,
                    0.0,
                    "R-TEMP-01: SOP §1 says contact the driver to restart the cooling unit",
                )
            )
            candidates.append(
                self._candidate(
                    TICKET,
                    incident.shipment_id,
                    30.0 if door_open else 10.0,
                    0.0,
                    "record the excursion"
                    + ("; triage says a door-open event that has recovered" if door_open else ""),
                )
            )
            for site in usable:
                if site.kind != "cold_storage" or site.available <= 0:
                    continue
                minutes = travel_minutes(lat, lon, site.lat, site.lon)
                harm = 110.0 if eta else 30.0
                reason = (
                    "R-ETA-01: SOP §1 says divert to the nearest emergency cold storage"
                    if eta
                    else "breach without a long delay: diversion is a weaker option"
                )
                candidates.append(
                    self._candidate(
                        "divert_to_cold_storage",
                        site.facility_id,
                        harm,
                        DELAY_COST_PER_MIN * minutes,
                        f"{reason}; {minutes:.0f} min away",
                    )
                )
        if "R-CONG-01" in rules:
            overflow = [s for s in usable if s.kind == "overflow_depot" and s.available > 0]
            for site in overflow:
                minutes = travel_minutes(lat, lon, site.lat, site.lon)
                candidates.append(
                    self._candidate(
                        "divert_to_overflow_depot",
                        site.facility_id,
                        100.0,
                        DELAY_COST_PER_MIN * minutes,
                        "R-CONG-01: SOP §2 says divert to the overflow depot; "
                        f"{minutes:.0f} min away",
                    )
                )
        if "R-RISK-01" in rules or temp:
            # A recovered door-open event needs no manager; a breach nobody can fix does.
            harm = 50.0 if "R-RISK-01" in rules else (10.0 if door_open else 40.0)
            reason = (
                "R-RISK-01: SOP §3 says escalate to the Tier 2 manager"
                if "R-RISK-01" in rules
                else "fallback if the breach cannot be fixed remotely"
            )
            candidates.append(
                self._candidate("escalate_tier2_manager", incident.shipment_id, harm, 0.0, reason)
            )

        ranked = tuple(sorted(candidates, key=lambda c: (-c.utility, c.action, c.target)))
        close = len(ranked) >= 2 and ranked[0].utility - ranked[1].utility < self.margin
        companions: tuple[Candidate, ...] = ()
        if temp:
            companions = (
                self._candidate(TICKET, incident.shipment_id, 10.0, 0.0, "record the excursion"),
            )
        return Plan(incident.incident_id, ranked, companions, close, confidence, tuple(notes))

    def proposal(
        self,
        plan: Plan,
        chosen: Candidate,
        incident: Incident,
        reading: RuleInput,
        now: datetime,
        *,
        planner: Literal["deterministic", "llm"] = "deterministic",
        degraded: bool = False,
        note: str = "",
    ) -> Proposal:
        evidence = tuple(
            Evidence(
                rule_id=h.rule_id,
                seq=h.seq,
                reading_ts=h.reading_ts,
                dq_status=h.dq_status,
                sop_ref=h.sop_ref,
                observed=dict(h.observed),
            )
            for h in incident.latest_evidence
        )
        alternatives = tuple(c.describe() for c in plan.candidates if c != chosen)
        return Proposal(
            incident_id=incident.incident_id,
            shipment_id=incident.shipment_id,
            depot_id=incident.depot_id,
            action=chosen.action,
            target=chosen.target,
            rule_ids=incident.rule_ids,
            evidence=evidence,
            eta_delay_min=reading.eta_delay_min,
            alternatives=alternatives,
            rationale=(
                f"{chosen.reason}. Utility {chosen.utility:.1f} = harm avoided "
                f"{chosen.harm_avoided:.0f} - delay {chosen.delay_cost:.1f} - "
                f"irreversibility {chosen.irreversibility_cost:.0f}."
                + (" Close call." if plan.close_call else "")
                + (" Low confidence: " + "; ".join(plan.notes) if plan.notes else "")
                + (f" {note}" if note else "")
            ),
            planner=planner,
            degraded=degraded,
            created_at=now,
        )
