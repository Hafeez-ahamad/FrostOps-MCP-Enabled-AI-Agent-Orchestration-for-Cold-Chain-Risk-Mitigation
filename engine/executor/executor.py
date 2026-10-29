"""Policy-gated, idempotent executor (deterministic; no LLM).

States: queued -> executing -> succeeded | failed | cancelled.

- enqueue() only accepts an EXECUTE decision, and checks the database itself: the proposal
  must be stored as approved with the same hash, and an L3 action needs a stored human
  approval of that hash. A forged or stale GateDecision object therefore cannot run anything.
- One action per idempotency key (incident:action:target), backed by a unique index.
- run_once() is one polling interval. If the kill switch is on it executes nothing, and it
  re-checks the switch before every action, so the switch takes effect within one poll.
- The audit entry "action_executing" is written before each call. If it fails, the call is
  not made and the action stays queued (fail closed).
- Retryable failures back off exponentially in simulated time; after `max_attempts` the action
  fails and the caller escalates (see engine/dispatch.py).
- cancel(): a queued action is simply cancelled; a succeeded reversible one is compensated
  (close the ticket, cancel the diversion, tell the driver to disregard); an irreversible
  succeeded action cannot be cancelled.
"""

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import Engine

from engine.actions.models import ActionRecord, ActionStatus, GateDecision, GateOutcome
from engine.actions.policy import ActionCatalog, ActionLevel, Reversibility
from engine.audit.log import AuditLog, AuditUnavailable
from engine.integrations.clients import CallResult, Integrations
from engine.observability import ACTIONS, log_event, raise_alert
from engine.ops_store import ActionStore, ApprovalStore, ControlStore, ProposalStore

ACTOR = "executor"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 60.0


class ExecutionRefused(RuntimeError):
    """The executor will not take this action (not approved by the gate, or not cancellable)."""


def _action_id(idempotency_key: str) -> str:
    return "ACT-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


class Executor:
    def __init__(
        self,
        engine: Engine,
        catalog: ActionCatalog,
        integrations: Integrations,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        backoff_seconds: float = BACKOFF_SECONDS,
    ) -> None:
        self.catalog = catalog
        self.integrations = integrations
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.audit = AuditLog(engine)
        self.actions = ActionStore(engine)
        self.proposals = ProposalStore(engine)
        self.approvals = ApprovalStore(engine)
        limits = catalog.global_limits
        self.controls = ControlStore(
            engine, {"kill_switch": limits.kill_switch, "dry_run": limits.dry_run}
        )

    # --- authorisation, checked against the database, never against the caller's word -------

    def _authorised(self, proposal_id: str, proposal_hash: str) -> tuple[bool, str]:
        row = self.proposals.get(proposal_id)
        if row is None:
            return False, "proposal not found"
        if row["Status"] != "approved":
            return False, f"proposal status is {row['Status']}"
        if row["ProposalHash"] != proposal_hash:
            return False, "proposal hash mismatch"
        policy = self.catalog.get(row["Action"])
        if policy is None or policy.level not in (ActionLevel.L2, ActionLevel.L3):
            return False, "action level is not executable"
        if policy.level is ActionLevel.L3:
            approval = self.approvals.for_proposal(proposal_id)
            if approval is None or approval["Decision"] != "approve":
                return False, "no recorded human approval"
            if approval["ProposalHash"] != proposal_hash:
                return False, "approval does not match the proposal hash"
        return True, ""

    def enqueue(self, d: GateDecision, now: datetime) -> ActionRecord:
        if d.outcome is not GateOutcome.EXECUTE:
            raise ExecutionRefused(f"gate outcome is {d.outcome.value}")
        ok, why = self._authorised(d.proposal_id, d.proposal_hash)
        if not ok:
            self.audit.append(
                "enqueue_refused", ACTOR, now, proposal_id=d.proposal_id, detail={"why": why}
            )
            raise ExecutionRefused(why)
        p = self.proposals.proposal(d.proposal_id)
        assert p is not None
        existing = self.actions.by_key(p.idempotency_key)
        if existing is not None:
            return existing  # idempotent: the same incident never triggers the same action twice
        policy = self.catalog.get(p.action)
        assert policy is not None
        record = ActionRecord(
            action_id=_action_id(p.idempotency_key),
            idempotency_key=p.idempotency_key,
            proposal_id=p.proposal_id,
            proposal_hash=d.proposal_hash,
            incident_id=p.incident_id,
            shipment_id=p.shipment_id,
            action=p.action,
            target=p.target,
            level=policy.level,
            status=ActionStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        self.audit.append(
            "action_enqueued",
            ACTOR,
            now,
            proposal_id=p.proposal_id,
            action_id=record.action_id,
            detail={"idempotency_key": p.idempotency_key, "approval_id": d.approval_id},
        )
        self.actions.insert(record)
        return record

    # --- one polling interval ----------------------------------------------------------------

    def run_once(self, now: datetime) -> list[ActionRecord]:
        """Process due actions. Returns the records that changed."""
        if self.controls.is_on("kill_switch"):
            return []
        changed = []
        for record in self.actions.due(now):
            if self.controls.is_on("kill_switch"):
                break
            changed.append(self._execute(record, now))
        return changed

    def _execute(self, record: ActionRecord, now: datetime) -> ActionRecord:
        ok, why = self._authorised(record.proposal_id, record.proposal_hash)
        if not ok:
            # An action row that the gate never approved (e.g. inserted directly) never runs.
            refused = record.model_copy(
                update={
                    "status": ActionStatus.FAILED,
                    "last_error": f"refused: {why}",
                    "updated_at": now,
                }
            )
            try:
                self.audit.append(
                    "action_refused", ACTOR, now, action_id=record.action_id, detail={"why": why}
                )
            except AuditUnavailable:
                pass  # refusing is already the safe outcome
            self.actions.save(refused)
            ACTIONS.labels(status="refused", action=record.action).inc()
            return refused
        attempt = record.attempts + 1
        try:
            self.audit.append(
                "action_executing",
                ACTOR,
                now,
                proposal_id=record.proposal_id,
                action_id=record.action_id,
                detail={"attempt": attempt, "action": record.action},
            )
        except AuditUnavailable:
            raise_alert(
                "audit_unavailable",
                "audit write failed: action held, integration not called",
                correlation_id=f"COR-{record.incident_id}",
                action_id=record.action_id,
            )
            held = record.model_copy(update={"last_error": "audit_unavailable", "updated_at": now})
            self.actions.save(held)  # stays queued: nothing ran
            return held

        executing = record.model_copy(
            update={"status": ActionStatus.EXECUTING, "attempts": attempt, "updated_at": now}
        )
        self.actions.save(executing)
        result = self._call(executing)

        if result.ok:
            done = executing.model_copy(
                update={"status": ActionStatus.SUCCEEDED, "result": result.data, "last_error": None}
            )
            event = "action_succeeded"
        elif result.retryable and attempt < self.max_attempts:
            wait = timedelta(seconds=self.backoff_seconds * 2 ** (attempt - 1))
            done = executing.model_copy(
                update={
                    "status": ActionStatus.QUEUED,
                    "next_attempt_at": now + wait,
                    "last_error": result.error,
                }
            )
            event = "action_retry_scheduled"
        else:
            done = executing.model_copy(
                update={"status": ActionStatus.FAILED, "last_error": result.error}
            )
            event = "action_failed"
        self.actions.save(done)
        try:
            self.audit.append(
                event,
                ACTOR,
                now,
                proposal_id=record.proposal_id,
                action_id=record.action_id,
                detail={"attempt": attempt, "error": result.error, "result": result.data},
            )
        except AuditUnavailable:
            pass  # the step already happened and is stored in ops.Actions; the gap is visible
        ACTIONS.labels(status=event.removeprefix("action_"), action=record.action).inc()
        if done.status is ActionStatus.FAILED:
            raise_alert(
                "action_failed",
                f"{record.action} failed after {attempt} attempt(s)",
                correlation_id=f"COR-{record.incident_id}",
                action_id=record.action_id,
                error=result.error,
            )
        log_event(
            "executor",
            event,
            correlation_id=f"COR-{record.incident_id}",
            action_id=record.action_id,
            action=record.action,
            attempt=attempt,
            error=result.error,
        )
        return done

    def _call(self, r: ActionRecord) -> CallResult:
        policy = self.catalog.get(r.action)
        key = r.idempotency_key
        svc = self.integrations
        if policy is None or policy.integration is None:
            return CallResult(False, error="no integration")
        if policy.integration == "driver_notify":
            text = (
                f"Cold-chain breach on {r.shipment_id}: restart or reset the reefer unit (SOP §1)."
            )
            return svc.driver.notify(r.target, text, key)
        if policy.integration == "ticketing":
            return svc.ticketing.create_ticket(
                r.incident_id, r.shipment_id, f"Cold-chain incident {r.incident_id}", key
            )
        if policy.integration == "tms_diversion":
            return svc.tms.divert(r.shipment_id, r.target, key)
        return svc.ticketing.escalate(
            r.incident_id, r.shipment_id, f"Tier 2 escalation for {r.incident_id}", key
        )

    # --- cancel and compensate -----------------------------------------------------------------

    def cancel(self, action_id: str, actor: str, now: datetime) -> ActionRecord:
        record = self.actions.get(action_id)
        if record is None:
            raise ExecutionRefused("unknown action")
        policy = self.catalog.get(record.action)
        assert policy is not None
        if record.status is ActionStatus.QUEUED:
            self.audit.append(
                "action_cancelled", actor, now, action_id=action_id, detail={"was": "queued"}
            )
            cancelled = record.model_copy(
                update={"status": ActionStatus.CANCELLED, "updated_at": now}
            )
            self.actions.save(cancelled)
            return cancelled
        if record.status is not ActionStatus.SUCCEEDED:
            raise ExecutionRefused(f"cannot cancel a {record.status.value} action")
        if policy.reversible is Reversibility.NO:
            raise ExecutionRefused(f"{record.action} is not reversible")
        self.audit.append("action_compensating", actor, now, action_id=action_id, detail={})
        result = self._compensate(record)
        if not result.ok:
            raise ExecutionRefused(f"compensation failed: {result.error}")
        cancelled = record.model_copy(
            update={
                "status": ActionStatus.CANCELLED,
                "updated_at": now,
                "result": {**record.result, "compensation": result.data},
            }
        )
        self.actions.save(cancelled)
        self.audit.append(
            "action_cancelled", actor, now, action_id=action_id, detail={"was": "succeeded"}
        )
        return cancelled

    def _compensate(self, r: ActionRecord) -> CallResult:
        svc = self.integrations
        if r.action == "create_incident_ticket":
            return svc.ticketing.close_ticket(str(r.result["ticket_id"]))
        if r.action in ("divert_to_cold_storage", "divert_to_overflow_depot"):
            return svc.tms.cancel(str(r.result["diversion_id"]))
        if r.action == "notify_driver_restart_cooling":
            return svc.driver.notify(
                r.target, "Disregard the previous instruction.", r.idempotency_key + ":undo"
            )
        return CallResult(False, error="no compensation defined")
