"""A small world for gate and executor tests: SQLite, in-process mocks, gate, executor."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine

from engine.actions.models import Approval, Evidence, GateDecision, Proposal
from engine.actions.policy import ActionCatalog, load_action_catalog
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from engine.incidents.store import save_rule_hits
from engine.integrations.clients import Integrations
from engine.rules.engine import RuleHit
from sim.labels import WorldEvent
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from tests.fakes import sqlite_engine

T0 = datetime(2026, 3, 2, 12, 0)

# Which rule each action rests on, for building realistic proposals.
RULE_FOR = {
    "notify_driver_restart_cooling": "R-TEMP-01",
    "create_incident_ticket": "R-TEMP-01",
    "divert_to_cold_storage": "R-ETA-01",
    "divert_to_overflow_depot": "R-CONG-01",
    "escalate_tier2_manager": "R-RISK-01",
    "reject_load": "R-TEMP-01",
}


@dataclass
class Harness:
    engine: Engine
    world: MockWorld
    faults: FaultInjector
    integrations: Integrations
    catalog: ActionCatalog
    gate: PolicyGate
    executor: Executor

    @classmethod
    def build(cls, catalog: ActionCatalog | None = None) -> "Harness":
        engine = sqlite_engine()
        world = MockWorld(now=T0, port_congestion={"POLB": 3.0})
        faults = FaultInjector()
        integrations = in_process_integrations(world, faults)
        catalog = catalog or load_action_catalog()
        return cls(
            engine,
            world,
            faults,
            integrations,
            catalog,
            PolicyGate(engine, catalog, integrations),
            Executor(engine, catalog, integrations),
        )

    def proposal(
        self,
        action: str,
        *,
        target: str | None = None,
        shipment: str = "S1",
        incident: str = "INC-S1-01",
        dq: str = "valid",
        eta: float | None = 0.0,
        record: bool = True,
        seq: int = 7,
        at: datetime = T0,
    ) -> Proposal:
        """A proposal whose evidence is (optionally) recorded in ops.RuleHits with `dq`."""
        rule = RULE_FOR.get(action, "R-TEMP-01")
        rule_ids = ("R-TEMP-01", "R-ETA-01") if rule == "R-ETA-01" else (rule,)
        hits = [
            RuleHit(
                rule_id=r,
                shipment_id=shipment,
                seq=seq,
                reading_ts=at,
                dq_status=dq,  # type: ignore[arg-type]
                sop_clause="§1",
                sop_ref="cold_chain_sop.md §1",
                observed={"temperature_c": 6.0},
            )
            for r in rule_ids
        ]
        if record:
            with self.engine.begin() as conn:
                save_rule_hits(conn, hits, at)
        default_target = {
            "divert_to_cold_storage": "CS-VENTURA",
            "divert_to_overflow_depot": "IE-OVERFLOW",
        }.get(action, shipment)
        return Proposal(
            incident_id=incident,
            shipment_id=shipment,
            depot_id="DEP-LB",
            action=action,
            target=target or default_target,
            rule_ids=rule_ids,
            evidence=tuple(
                Evidence(
                    rule_id=h.rule_id, seq=h.seq, reading_ts=at, dq_status=dq, sop_ref=h.sop_ref
                )  # type: ignore[arg-type]
                for h in hits
            ),
            eta_delay_min=eta,
            rationale="test",
            planner="deterministic",
            created_at=at,
        )

    def approval(
        self,
        p: Proposal,
        *,
        role: str = "dispatcher",
        approver: str = "alice",
        decision: str = "approve",
        at: datetime = T0,
        approval_id: str | None = None,
        proposal_hash: str | None = None,
    ) -> Approval:
        return Approval(
            approval_id=approval_id or f"APR-{p.proposal_id}-{approver}",
            proposal_id=p.proposal_id,
            proposal_hash=proposal_hash or p.content_hash(),
            approver_id=approver,
            approver_role=role,
            decision=decision,  # type: ignore[arg-type]
            decided_at=at,
        )

    def submit_and_enqueue(self, p: Proposal, at: datetime = T0) -> GateDecision:
        d = self.gate.submit(p, at)
        if d.outcome.value == "execute":
            self.executor.enqueue(d, at)
        return d

    def unreachable(self, shipment: str = "S1", hours: float = 24) -> None:
        self.world.events.append(
            WorldEvent(
                kind="driver_unreachable",
                subject_id=shipment,
                start_ts=T0,
                end_ts=T0 + timedelta(hours=hours),
            )
        )

    def facility_full(self, facility: str, hours: float = 24) -> None:
        self.world.events.append(
            WorldEvent(
                kind="facility_full",
                subject_id=facility,
                start_ts=T0,
                end_ts=T0 + timedelta(hours=hours),
            )
        )
