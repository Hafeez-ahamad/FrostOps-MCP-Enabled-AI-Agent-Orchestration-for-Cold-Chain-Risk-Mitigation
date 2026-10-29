"""Named preconditions checked by the policy gate (deterministic; no LLM).

The gate does not trust the evidence inside a proposal. `rule_breach_confirmed` and
`data_quality_ok` look the cited rule hits up in ops.RuleHits, where the detection pipeline
wrote them with each reading's data-quality status. Checks that call a service fail closed:
if the service cannot answer, the precondition is not met.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select

from engine.actions.models import Evidence, Proposal
from engine.actions.policy import Precondition
from engine.data.schema import DATA_QUALITY_FLAGS, RULE_HITS
from engine.integrations.clients import FacilityList, Integrations, Reachability

ETA_DELAY_THRESHOLD_MIN = 60.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


class PreconditionChecker:
    def __init__(self, engine: Engine, integrations: Integrations) -> None:
        self.engine = engine
        self.integrations = integrations

    def _recorded_status(self, shipment_id: str, e: Evidence) -> str | None:
        """The data-quality status stored with this rule hit, or None if it was never recorded."""
        with self.engine.connect() as conn:
            return conn.execute(
                select(RULE_HITS.c.DqStatus)
                .where(RULE_HITS.c.ShipmentID == shipment_id)
                .where(RULE_HITS.c.RuleId == e.rule_id)
                .where(RULE_HITS.c.MsgSeqNo == e.seq)
                .limit(1)
            ).scalar_one_or_none()

    def _went_stale_after(self, shipment_id: str, since: datetime) -> bool:
        with self.engine.connect() as conn:
            found = conn.execute(
                select(DATA_QUALITY_FLAGS.c.FlagID)
                .where(DATA_QUALITY_FLAGS.c.ShipmentID == shipment_id)
                .where(DATA_QUALITY_FLAGS.c.CheckName == "freshness")
                .where(DATA_QUALITY_FLAGS.c.EvaluatedAt > since)
                .limit(1)
            ).first()
            return found is not None

    def check(self, name: Precondition, p: Proposal) -> CheckResult:
        if name == "rule_breach_confirmed":
            confirmed = [
                e
                for e in p.evidence
                if e.rule_id in p.rule_ids and self._recorded_status(p.shipment_id, e) == "valid"
            ]
            if confirmed:
                return CheckResult(name, True, f"{len(confirmed)} recorded valid hit(s)")
            return CheckResult(name, False, "no recorded rule hit on a valid reading")

        if name == "data_quality_ok":
            if not p.evidence:
                return CheckResult(name, False, "no evidence")
            for e in p.evidence:
                recorded = self._recorded_status(p.shipment_id, e)
                if recorded != "valid":
                    return CheckResult(
                        name, False, f"{e.rule_id} seq {e.seq} recorded as {recorded}"
                    )
            latest = max((e.reading_ts for e in p.evidence if e.reading_ts), default=None)
            if latest is not None and self._went_stale_after(p.shipment_id, latest):
                return CheckResult(name, False, "shipment telemetry went stale after the evidence")
            return CheckResult(name, True)

        if name == "driver_reachable":
            result = self.integrations.driver.reachable(p.shipment_id)
            if not result.ok:
                return CheckResult(name, False, f"dependency_unavailable: {result.error}")
            reachable = Reachability.model_validate(result.data).reachable
            return CheckResult(name, reachable, "" if reachable else "driver_unreachable")

        if name == "facility_capacity_ok":
            result = self.integrations.facilities.list()
            if not result.ok:
                return CheckResult(name, False, f"dependency_unavailable: {result.error}")
            sites = {f.facility_id: f for f in FacilityList.model_validate(result.data).facilities}
            site = sites.get(p.target)
            if site is None:
                return CheckResult(name, False, f"unknown facility {p.target}")
            return CheckResult(name, site.available > 0, f"{site.available} slot(s) available")

        if name == "eta_delay_gt_60min":
            ok = p.eta_delay_min is not None and p.eta_delay_min > ETA_DELAY_THRESHOLD_MIN
            return CheckResult(name, ok, f"ETA delay {p.eta_delay_min}")

        return CheckResult(name, False, "unknown precondition")  # default deny
