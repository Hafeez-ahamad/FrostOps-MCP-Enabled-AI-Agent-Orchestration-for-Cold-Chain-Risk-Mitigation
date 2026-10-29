"""The engine loop on a virtual clock, for any configuration (C0, C1, C2).

Readings arrive in receive order and go through data quality, rules and incidents; incident
events go to the configuration's decision graph (engine/dispatch.py). At every tick (one
polling interval) the loop advances the outside world (`on_tick`), runs the freshness check,
offers pending L3 proposals to `approver` (a stand-in for a human), and runs the executor once.

Each role can run on its own database login (`RoleEngines`, db/roles.sql): the monitor writes
rule hits and data-quality flags, the gate (and the message bus) writes proposals, the executor
writes actions. A single `Engine` gives every role the same connection (tests, replay, demo).

Alert-only mode (spec section 14) is a safer mode, never a more permissive one. It starts when a
database write still fails after `DB_ATTEMPTS` tries, or when a reading arrives without the
legacy columns (schema drift; such a reading is refused, since it cannot be checked). Incidents
are still detected and alerted, but nothing is dispatched and the executor does not run.
Only a person can leave it (`clear_alert_only`).
"""

import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, insert
from sqlalchemy.exc import DBAPIError

from engine.actions.models import Approval, GateOutcome, Proposal
from engine.actions.policy import ActionCatalog, load_action_catalog
from engine.agents.context import ReadingPoint
from engine.bus.messages import ActionApproved, correlation_id
from engine.bus.queue import MessageBus
from engine.data.schema import DATA_QUALITY_FLAGS, RAW_COLUMNS
from engine.dispatch import Dispatcher
from engine.dq.checks import DataQualityChecker, DqStatus, Reading, parse_ts
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from engine.graphs import CONFIGURATIONS
from engine.graphs.nodes import DecisionContext
from engine.incidents.manager import IncidentManager
from engine.incidents.store import save_rule_hits
from engine.integrations.clients import Integrations
from engine.llm.structured import StructuredLLM
from engine.observability import ALERT_ONLY, INCIDENTS_OPENED, log_event, raise_alert
from engine.planner.planner import DeterministicPlanner
from engine.rules.engine import DqStatusText, RuleInput, RulesEngine
from engine.sop.retriever import LocalSopRetriever

Approver = Callable[[list[Proposal], datetime], list[Approval]]
Configuration = Literal["C0", "C1", "C2"]
WINDOW_READINGS = 8
DB_ATTEMPTS = 3
# Columns every reading must carry; LOAD_FLG is set by ingestion, not by the source.
REQUIRED_COLUMNS = tuple(c for c in RAW_COLUMNS if c != "LOAD_FLG")


def received_at(row: Mapping[str, Any]) -> datetime | None:
    """When a reading was received; tolerates a reading with other columns missing."""
    return parse_ts(row.get("RCPT_DTTM"))


@dataclass(frozen=True)
class RoleEngines:
    """One database engine per agent role (each its own login in production, db/roles.sql)."""

    monitor: Engine  # rule hits, incidents, data-quality flags
    planner: Engine  # LLM call log (drafts go through the gate's procedure)
    gate: Engine  # proposals, approvals, controls, audit; also the message bus
    executor: Engine  # actions
    explainer: Engine  # read-only

    @classmethod
    def single(cls, engine: Engine) -> "RoleEngines":
        return cls(engine, engine, engine, engine, engine)


@dataclass
class Runtime:
    engines: RoleEngines
    config: Configuration
    integrations: Integrations
    catalog: ActionCatalog
    checker: DataQualityChecker
    rules: RulesEngine
    manager: IncidentManager
    gate: PolicyGate
    executor: Executor
    dispatcher: Dispatcher
    windows: dict[str, deque[ReadingPoint]] = field(default_factory=dict)
    alert_only: bool = False
    alert_reason: str | None = None

    @classmethod
    def build(
        cls,
        engine: Engine | RoleEngines,
        integrations: Integrations,
        catalog: ActionCatalog | None = None,
        *,
        config: Configuration = "C0",
        llm: StructuredLLM | None = None,
        explain: bool = False,
        sop: LocalSopRetriever | None = None,
    ) -> "Runtime":
        engines = engine if isinstance(engine, RoleEngines) else RoleEngines.single(engine)
        catalog = catalog or load_action_catalog()
        manager = IncidentManager()
        gate = PolicyGate(engines.gate, catalog, integrations)
        executor = Executor(engines.executor, catalog, integrations)
        ctx = DecisionContext(
            planner=DeterministicPlanner(catalog),
            gate=gate,
            executor=executor,
            bus=MessageBus(engines.gate),
            llm=llm,
            explain=explain,
            sop=sop or LocalSopRetriever(),
        )
        graph = CONFIGURATIONS[config](ctx)
        dispatcher = Dispatcher(ctx, graph, integrations, manager)
        ALERT_ONLY.set(0)
        return cls(
            engines,
            config,
            integrations,
            catalog,
            DataQualityChecker(),
            RulesEngine(),
            manager,
            gate,
            executor,
            dispatcher,
        )

    @property
    def engine(self) -> Engine:
        """The monitor's engine (detection writes); for callers that need a single engine."""
        return self.engines.monitor

    @property
    def degraded_steps(self) -> list[str]:
        return self.dispatcher.ctx.degraded_steps

    # --- alert-only mode -----------------------------------------------------------------------

    def enter_alert_only(self, reason: str, now: datetime) -> None:
        if self.alert_only:
            return
        self.alert_only, self.alert_reason = True, reason
        ALERT_ONLY.set(1)
        raise_alert("alert_only", f"entering alert-only mode: {reason}", at=now.isoformat())

    def clear_alert_only(self, actor: str, now: datetime) -> None:
        """A person confirms the cause is fixed. Audited first; raises if the audit fails."""
        self.gate.audit.append(
            "alert_only_cleared", actor, now, detail={"reason": self.alert_reason}
        )
        self.alert_only, self.alert_reason = False, None
        ALERT_ONLY.set(0)

    def _write(self, what: str, write: Callable[[], None], now: datetime) -> bool:
        """A detection write with retries (immediate: the clock is virtual). After DB_ATTEMPTS
        failures the engine goes alert-only. OperationalError is a DBAPIError."""
        for attempt in range(1, DB_ATTEMPTS + 1):
            try:
                write()
                return True
            except DBAPIError as exc:
                log_event(
                    "runtime",
                    f"{what} failed",
                    level=logging.WARNING,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
        self.enter_alert_only(f"database unavailable ({what})", now)
        return False

    # --- the loop ------------------------------------------------------------------------------

    def process_reading(self, row: Mapping[str, Any], now: datetime) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in row]
        if missing:
            # The reading cannot be checked or evaluated: refuse it, loudly, and stop acting.
            self.enter_alert_only(f"schema drift: reading lacks {missing[:5]}", now)
            raise_alert(
                "schema_drift",
                "reading refused: legacy columns missing",
                shipment_id=row.get("CNSG_NO"),
                missing=missing,
            )
            return
        assessment = self.checker.assess(Reading.from_legacy(row))
        status: DqStatusText = assessment.status.value
        reading = RuleInput.from_legacy(row)
        if assessment.status is DqStatus.VALID and reading.reading_ts is not None:
            window = self.windows.setdefault(reading.shipment_id, deque(maxlen=WINDOW_READINGS))
            window.append(
                ReadingPoint(reading.reading_ts, reading.temperature_c, reading.eta_delay_min)
            )
        hits = self.rules.evaluate(reading, status)
        if hits:

            def save_hits() -> None:
                with self.engines.monitor.begin() as conn:
                    save_rule_hits(conn, hits, now)

            self._write("save rule hits", save_hits, now)
        event = self.manager.process(reading, status, hits)
        if event is not None and event.kind == "opened":
            cor = correlation_id(event.incident.incident_id)
            INCIDENTS_OPENED.inc()
            log_event(
                "monitor",
                "incident opened",
                correlation_id=cor,
                shipment_id=reading.shipment_id,
                rules=list(event.incident.rule_ids),
            )
            if self.alert_only:
                raise_alert(
                    "incident_in_alert_only",
                    "incident detected in alert-only mode; nothing dispatched",
                    correlation_id=cor,
                )
        if event is not None and assessment.status is DqStatus.VALID and not self.alert_only:
            window_now = list(self.windows.get(reading.shipment_id, ()))
            self.dispatcher.on_event(event, reading, now, window_now)

    def tick(self, now: datetime, approver: Approver | None = None) -> None:
        stale = self.checker.check_freshness(now)
        if stale:

            def save_flags() -> None:
                with self.engines.monitor.begin() as conn:
                    conn.execute(
                        insert(DATA_QUALITY_FLAGS),
                        [
                            {
                                "ShipmentID": f.shipment_id,
                                "MsgSeqNo": f.seq,
                                "CheckName": f.check,
                                "DqStatus": f.status.value,
                                "Detail": f.detail,
                                "EvaluatedAt": f.evaluated_at,
                            }
                            for f in stale
                        ],
                    )

            self._write("save freshness flags", save_flags, now)
        if self.alert_only:
            return  # no approval is acted on and the executor does not run
        if approver is not None:
            pending = [
                p
                for r in self.gate.proposals.pending()
                if (p := self.gate.proposals.proposal(r["ProposalID"]))
            ]
            for approval in approver(pending, now):
                decision = self.gate.decide(approval, now)
                self.dispatcher.decisions.append(decision)
                if decision.outcome is GateOutcome.EXECUTE:
                    proposal = self.gate.proposals.proposal(approval.proposal_id)
                    assert proposal is not None
                    self.dispatcher.ctx.bus.publish(
                        correlation_id(proposal.incident_id),
                        "approver",
                        ActionApproved(
                            incident_id=proposal.incident_id,
                            proposal_id=proposal.proposal_id,
                            action=proposal.action,
                            level=decision.level.value if decision.level else "?",
                            approved_by=f"{approval.approver_role}:{approval.approver_id}",
                        ),
                        now,
                    )
                    self.executor.enqueue(decision, now)
        changed = self.executor.run_once(now)
        self.dispatcher.on_actions(changed, now)

    def run(
        self,
        rows: Sequence[Mapping[str, Any]],
        tick: timedelta,
        end: datetime,
        *,
        on_tick: Callable[[datetime], None] | None = None,
        approver: Approver | None = None,
    ) -> None:
        next_tick: datetime | None = None
        for row in rows:
            received = received_at(row)
            if received is None:
                continue
            if next_tick is None:
                next_tick = datetime.min + ((received - datetime.min) // tick + 1) * tick
            while next_tick <= received:
                if on_tick:
                    on_tick(next_tick)
                self.tick(next_tick, approver)
                next_tick += tick
            self.process_reading(row, received)
        while next_tick is not None and next_tick <= end:
            if on_tick:
                on_tick(next_tick)
            self.tick(next_tick, approver)
            next_tick += tick
