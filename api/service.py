"""The engine behind the API: a runtime stepping through a simulated day on demand.

`advance(minutes)` feeds the readings received in that window through detection and dispatch
and runs the executor at every tick, exactly like the replay loop, but on request. Human
approvals come from the API instead of a simulated dispatcher. One lock serialises access.
"""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select

from engine.actions.models import Approval, GateDecision, GateOutcome
from engine.actions.policy import ACTIONS_PATH
from engine.data.schema import AUDIT_LOG, INCIDENTS, RULE_HITS
from engine.gate.policy_admin import PolicyAdmin
from engine.incidents.store import save_incidents
from engine.observability import KILL_SWITCH, QUEUE_DEPTH
from engine.runtime import Runtime, received_at
from sim.generator import SimulationOutput
from sim.mock_services.state import MockWorld


@dataclass
class EngineService:
    runtime: Runtime
    sim: SimulationOutput
    mock: MockWorld
    now: datetime
    tick: timedelta = timedelta(minutes=5)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _rows: list[dict[str, Any]] = field(default_factory=list)
    _pos: int = 0
    _series: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    policy: PolicyAdmin = field(init=False)

    def __post_init__(self) -> None:
        self._rows = [
            {str(k): v for k, v in r.items()} for r in self.sim.readings.to_dict("records")
        ]
        self.policy = PolicyAdmin(
            self.runtime.engines.gate,
            self.runtime.catalog,
            ACTIONS_PATH.read_text(encoding="utf-8"),
        )

    @property
    def engine(self) -> Engine:
        return self.runtime.engine

    # --- clock ---------------------------------------------------------------------------

    def advance(self, minutes: float) -> datetime:
        """Move the simulated clock forward, processing everything received meanwhile."""
        with self.lock:
            target = self.now + timedelta(minutes=minutes)
            while self.now < target:
                step_end = min(self.now + self.tick, target)
                while self._pos < len(self._rows):
                    row = self._rows[self._pos]
                    received = received_at(row)
                    if received is None or received <= step_end:
                        self._pos += 1
                        if received is not None:
                            self._record(row)
                            self.runtime.process_reading(row, received)
                        continue
                    break
                self.now = step_end
                self.mock.now = step_end
                self.runtime.tick(step_end, approver=None)
            self._persist()
            return self.now

    def _record(self, row: dict[str, Any]) -> None:
        self._series[str(row["CNSG_NO"])].append(
            {"ts": row["EVT_DTTM"], "temperature_c": row["RFR_TMP_C"], "seq": row["PKT_SEQ"]}
        )

    def _persist(self) -> None:
        with self.engine.begin() as conn:
            save_incidents(conn, self.runtime.manager.incidents.values(), self.now)
        QUEUE_DEPTH.set(len(self.runtime.gate.proposals.pending()))
        KILL_SWITCH.set(int(self.runtime.gate.controls.is_on("kill_switch")))

    # --- reads -----------------------------------------------------------------------------

    def incidents(self) -> list[dict[str, Any]]:
        with self.lock, self.engine.connect() as conn:
            rows = conn.execute(
                select(INCIDENTS).order_by(INCIDENTS.c.RiskScore.desc(), INCIDENTS.c.OpenedAt)
            ).mappings()
            return [dict(r) for r in rows]

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        with self.lock, self.engine.connect() as conn:
            row = (
                conn.execute(select(INCIDENTS).where(INCIDENTS.c.IncidentID == incident_id))
                .mappings()
                .first()
            )
            if row is None:
                return None
            incident = dict(row)
            hits = conn.execute(
                select(RULE_HITS)
                .where(RULE_HITS.c.ShipmentID == incident["ShipmentID"])
                .order_by(RULE_HITS.c.RuleHitID)
            ).mappings()
            incident["rule_hits"] = [dict(h) for h in hits]
        gate = self.runtime.gate
        incident["proposals"] = [r for r in self._all_proposals() if r["IncidentID"] == incident_id]
        incident["actions"] = [
            a.model_dump(mode="json") for a in gate.actions.all() if a.incident_id == incident_id
        ]
        incident["timeline"] = list(self._series.get(incident["ShipmentID"], []))
        band = self.sim.cargo_types.get(incident.get("CargoType") or "")
        incident["band_max_c"] = band.max_c if band else None
        explanation = self.runtime.dispatcher.ctx.explanations.get(incident_id)
        incident["explanation"] = explanation.model_dump() if explanation else None
        return incident

    def _all_proposals(self) -> list[dict[str, Any]]:
        from engine.data.schema import PROPOSALS

        with self.engine.connect() as conn:
            rows = conn.execute(select(PROPOSALS).order_by(PROPOSALS.c.CreatedAt)).mappings()
            return [dict(r) for r in rows]

    def proposals(self, status: str | None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._all_proposals()
        return [r for r in rows if status is None or r["Status"] == status]

    def audit(self, event_type: str | None, limit: int) -> list[dict[str, Any]]:
        query = select(AUDIT_LOG).order_by(AUDIT_LOG.c.AuditID.desc()).limit(limit)
        if event_type:
            query = query.where(AUDIT_LOG.c.EventType == event_type)
        with self.lock, self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(query).mappings()]

    # --- human decisions and controls ------------------------------------------------------------

    def decide(self, approval: Approval) -> GateDecision:
        with self.lock:
            decision = self.runtime.gate.decide(approval, self.now)
            if decision.outcome is GateOutcome.EXECUTE:
                self.runtime.executor.enqueue(decision, self.now)
            self._persist()
            return decision

    def cancel(self, action_id: str, actor: str) -> dict[str, Any]:
        with self.lock:
            return self.runtime.executor.cancel(action_id, actor, self.now).model_dump(mode="json")

    def update_policy(self, yaml_text: str, actor: str, reason: str) -> dict[str, Any]:
        """Safety-check, version and apply a new action catalogue (raises PolicyRejected).
        The gate and the executor switch to it together, under the lock."""
        with self.lock:
            catalog = self.policy.update(yaml_text, actor, reason, self.now)
            self.runtime.catalog = catalog
            self.runtime.gate.set_catalog(catalog)
            self.runtime.executor.catalog = catalog
            self.runtime.dispatcher.ctx.planner.catalog = catalog
            return {"version": catalog.version, "policy_hash": catalog.policy_hash}

    def clear_alert_only(self, actor: str) -> None:
        with self.lock:
            self.runtime.clear_alert_only(actor, self.now)

    def set_control(self, name: str, on: bool, actor: str) -> None:
        with self.lock:
            self.runtime.gate.set_control(name, on, actor, self.now)  # type: ignore[arg-type]
            self._persist()
