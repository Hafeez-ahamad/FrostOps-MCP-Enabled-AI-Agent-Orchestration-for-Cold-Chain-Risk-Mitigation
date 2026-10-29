"""Policy gate (deterministic; no LLM). Nothing executes unless this says so.

submit(proposal), spec section 9:
  1. the proposal is a validated Pydantic model; a re-submission of the same
     (incident, action, target) is a DUPLICATE and changes nothing
  2. the action must be in policy/actions.yaml: unknown actions are rejected (default deny)
  3. kill switch -> reject; cooldown, per-shipment and global hourly limits -> reject
  4. every listed precondition must pass; every L2 action also needs data_quality_ok
  5. by level: L2 -> EXECUTE (or DRY_RUN), L3 -> AWAIT_APPROVAL, L0/L1/L4 -> reject
  6. the decision is written to the audit log first; if that fails, the answer is REJECTED

decide(approval), for L3:
  the proposal must be awaiting approval; the approval id must be new; its hash must equal
  the stored proposal hash; the approver's role must be allowed; it must be recent (TTL) and
  not older than the proposal. Then kill switch, limits and preconditions are checked again,
  because the world may have changed while the proposal waited.
"""

from datetime import datetime, timedelta

from sqlalchemy import Engine

from engine.actions.models import Approval, GateDecision, GateOutcome, Proposal
from engine.actions.policy import ActionCatalog, ActionLevel, ActionPolicy
from engine.audit.log import AuditLog, AuditUnavailable
from engine.gate.preconditions import PreconditionChecker
from engine.integrations.clients import Integrations
from engine.observability import GATE_DECISIONS, log_event, raise_alert
from engine.ops_store import (
    ActionStore,
    ApprovalStore,
    ControlName,
    ControlStore,
    ProposalStatus,
    ProposalStore,
    hour_before,
)

ACTOR = "policy_gate"
STATUS_FOR: dict[GateOutcome, ProposalStatus] = {
    GateOutcome.EXECUTE: "approved",
    GateOutcome.AWAIT_APPROVAL: "awaiting_approval",
    GateOutcome.REJECTED: "rejected",
    GateOutcome.DRY_RUN: "dry_run",
}


class PolicyGate:
    def __init__(self, engine: Engine, catalog: ActionCatalog, integrations: Integrations) -> None:
        self.catalog = catalog
        self.audit = AuditLog(engine)
        self.proposals = ProposalStore(engine)
        self.approvals = ApprovalStore(engine)
        self.actions = ActionStore(engine)
        limits = catalog.global_limits
        self.controls = ControlStore(
            engine, {"kill_switch": limits.kill_switch, "dry_run": limits.dry_run}
        )
        self.preconditions = PreconditionChecker(engine, integrations)

    # --- helpers ---------------------------------------------------------------------------

    def _decision(
        self,
        p: Proposal,
        outcome: GateOutcome,
        reasons: list[str],
        now: datetime,
        level: ActionLevel | None,
        approval_id: str | None = None,
    ) -> GateDecision:
        return GateDecision(
            proposal_id=p.proposal_id,
            proposal_hash=p.content_hash(),
            action=p.action,
            level=level,
            outcome=outcome,
            reasons=tuple(reasons),
            policy_version=self.catalog.version,
            policy_hash=self.catalog.policy_hash,
            decided_at=now,
            approval_id=approval_id,
        )

    def _bare_decision(self, proposal_id: str, reasons: list[str], now: datetime) -> GateDecision:
        """A rejection for something that is not a stored proposal."""
        return GateDecision(
            proposal_id=proposal_id,
            proposal_hash="",
            action="",
            level=None,
            outcome=GateOutcome.REJECTED,
            reasons=tuple(reasons),
            policy_version=self.catalog.version,
            policy_hash=self.catalog.policy_hash,
            decided_at=now,
        )

    @staticmethod
    def _audit_failed(d: GateDecision) -> bool:
        return "audit_unavailable" in d.reasons

    def _record(self, event: str, d: GateDecision, actor: str = ACTOR) -> GateDecision:
        """Audit first. If the audit write fails, nothing may proceed: reject."""
        try:
            self.audit.append(
                event,
                actor,
                d.decided_at,
                proposal_id=d.proposal_id,
                detail={
                    "outcome": d.outcome.value,
                    "reasons": list(d.reasons),
                    "action": d.action,
                    "level": d.level.value if d.level else None,
                    "proposal_hash": d.proposal_hash,
                    "policy_version": d.policy_version,
                    "policy_hash": d.policy_hash,
                    "approval_id": d.approval_id,
                },
            )
        except AuditUnavailable:
            raise_alert("audit_unavailable", "audit write failed: gate rejects", audit_event=event)
            d = d.model_copy(
                update={
                    "outcome": GateOutcome.REJECTED,
                    "reasons": (*d.reasons, "audit_unavailable"),
                }
            )
        GATE_DECISIONS.labels(outcome=d.outcome.value, action=d.action or "unknown").inc()
        log_event(
            "gate",
            event,
            proposal_id=d.proposal_id,
            action=d.action,
            outcome=d.outcome.value,
            reasons=list(d.reasons),
            actor=actor,
        )
        return d

    def _limits(self, p: Proposal, policy: ActionPolicy, now: datetime) -> list[str]:
        reasons = []
        if self.controls.is_on("kill_switch"):
            reasons.append("kill_switch")
        last = self.actions.last_created(p.action, p.shipment_id)
        if (
            policy.cooldown_minutes
            and last
            and now - last < timedelta(minutes=policy.cooldown_minutes)
        ):
            reasons.append("cooldown")
        hour = hour_before(now)
        if policy.max_per_shipment_per_hour is not None:
            used = self.actions.count_since(hour, action=p.action, shipment_id=p.shipment_id)
            if used >= policy.max_per_shipment_per_hour:
                reasons.append("shipment_rate_limit")
        if policy.level is ActionLevel.L2:
            if (
                self.actions.count_since(hour, level=ActionLevel.L2)
                >= self.catalog.global_limits.max_auto_actions_per_hour
            ):
                reasons.append("global_rate_limit")
        return reasons

    def _precondition_failures(self, p: Proposal, policy: ActionPolicy) -> list[str]:
        names = list(policy.preconditions)
        if policy.level is ActionLevel.L2 and "data_quality_ok" not in names:
            names.append("data_quality_ok")  # L2 always needs clean data (spec section 7.2)
        failures = []
        for name in names:
            result = self.preconditions.check(name, p)
            if not result.ok:
                failures.append(
                    f"precondition:{name}" + (f" ({result.detail})" if result.detail else "")
                )
        return failures

    # --- submit ------------------------------------------------------------------------------

    def submit(self, p: Proposal, now: datetime) -> GateDecision:
        existing = self.proposals.get(p.proposal_id)
        if existing is not None:
            d = self._decision(
                p, GateOutcome.DUPLICATE, [f"already {existing['Status']}"], now, None
            )
            return self._record("gate_duplicate", d)

        policy = self.catalog.get(p.action)
        if policy is None:
            d = self._decision(p, GateOutcome.REJECTED, ["unknown_action"], now, None)
            return self._record("gate_decision", d)
        d = self._record("gate_decision", self._evaluate(p, policy, now))
        if not self._audit_failed(d):
            self.proposals.insert(p, policy.level, STATUS_FOR[d.outcome], now)
        return d

    def _evaluate(self, p: Proposal, policy: ActionPolicy, now: datetime) -> GateDecision:
        level = policy.level
        if level in (ActionLevel.L0, ActionLevel.L1):
            reasons = ["not_executable_level"]
        elif level is ActionLevel.L4:
            reasons = ["never_autonomous"]
        else:
            reasons = self._limits(p, policy, now) + self._precondition_failures(p, policy)

        if reasons:
            outcome = GateOutcome.REJECTED
        elif level is ActionLevel.L3:
            outcome = GateOutcome.AWAIT_APPROVAL
        elif self.controls.is_on("dry_run"):
            outcome = GateOutcome.DRY_RUN
        else:
            outcome = GateOutcome.EXECUTE
        return self._decision(p, outcome, reasons, now, level)

    # --- drafts (MCP planning server -> action server) ------------------------------------------

    def store_draft(self, p: Proposal, now: datetime) -> None:
        """Keep a proposal as a draft: it does nothing until submit_draft() runs the gate."""
        if self.proposals.get(p.proposal_id) is not None:
            raise ValueError(f"{p.proposal_id} already exists")
        policy = self.catalog.get(p.action)
        level = policy.level if policy else ActionLevel.L4  # unknown actions are denied later
        self.audit.append(
            "draft_stored", "planner", now, proposal_id=p.proposal_id, detail={"action": p.action}
        )
        self.proposals.insert_draft(p, level, now)

    def submit_draft(self, proposal_id: str, now: datetime) -> GateDecision:
        """Run the same checks as submit() on a stored draft. The stored payload is re-hashed,
        so a draft edited in the database after it was written is rejected."""
        row = self.proposals.get(proposal_id)
        p = self.proposals.proposal(proposal_id)
        if row is None or p is None:
            return self._record(
                "gate_decision", self._bare_decision(proposal_id, ["unknown_proposal"], now)
            )
        if row["Status"] != "draft":
            d = self._decision(p, GateOutcome.DUPLICATE, [f"already {row['Status']}"], now, None)
            return self._record("gate_duplicate", d)
        if p.content_hash() != row["ProposalHash"]:
            d = self._decision(p, GateOutcome.REJECTED, ["proposal_hash_mismatch"], now, None)
            d = self._record("gate_decision", d)
            if not self._audit_failed(d):
                self.proposals.set_status(proposal_id, "rejected", now)
            return d
        policy = self.catalog.get(p.action)
        if policy is None:
            d = self._decision(p, GateOutcome.REJECTED, ["unknown_action"], now, None)
        else:
            d = self._evaluate(p, policy, now)
        d = self._record("gate_decision", d)
        if not self._audit_failed(d):
            self.proposals.set_status(proposal_id, STATUS_FOR[d.outcome], now)
        return d

    def set_catalog(self, catalog: ActionCatalog) -> None:
        """Swap in a new (already safety-checked) policy version."""
        self.catalog = catalog

    # --- human decision on an L3 proposal ---------------------------------------------------------

    def decide(self, a: Approval, now: datetime) -> GateDecision:
        actor = f"{a.approver_role}:{a.approver_id}"
        row = self.proposals.get(a.proposal_id)
        p = self.proposals.proposal(a.proposal_id)
        if row is None or p is None:
            d = self._bare_decision(a.proposal_id, ["unknown_proposal"], now)
            return self._record("approval_rejected", d, actor)

        policy = self.catalog.get(p.action)
        level = policy.level if policy else None
        reasons: list[str] = []
        if row["Status"] != "awaiting_approval":
            reasons.append(f"not_awaiting_approval ({row['Status']})")
        if self.approvals.exists(a.approval_id) or self.approvals.for_proposal(a.proposal_id):
            reasons.append("approval_reused")
        if a.proposal_hash != row["ProposalHash"]:
            reasons.append("proposal_hash_mismatch")
        if policy is None or a.approver_role not in policy.approver_roles:
            reasons.append("approver_role_not_allowed")
        ttl = timedelta(minutes=self.catalog.global_limits.approval_ttl_minutes)
        if a.decided_at < row["CreatedAt"] or now - a.decided_at > ttl or a.decided_at > now:
            reasons.append("stale_or_future_approval")
        if reasons:
            d = self._decision(p, GateOutcome.REJECTED, reasons, now, level, a.approval_id)
            return self._record("approval_rejected", d, actor)

        assert policy is not None
        if a.decision == "reject":
            d = self._record(
                "approval_recorded",
                self._decision(
                    p, GateOutcome.REJECTED, ["rejected_by_human"], now, level, a.approval_id
                ),
                actor,
            )
            if self._audit_failed(d):
                return d
            self.approvals.insert(a, now)
            self.proposals.set_status(p.proposal_id, "rejected", now)
            return d

        recheck = self._limits(p, policy, now) + self._precondition_failures(p, policy)
        if recheck:
            outcome = GateOutcome.REJECTED
        elif self.controls.is_on("dry_run"):
            outcome = GateOutcome.DRY_RUN
        else:
            outcome = GateOutcome.EXECUTE
        d = self._record(
            "approval_recorded",
            self._decision(p, outcome, recheck, now, level, a.approval_id),
            actor,
        )
        if self._audit_failed(d):
            return d
        self.approvals.insert(a, now)
        self.proposals.set_status(p.proposal_id, STATUS_FOR[d.outcome], now)
        return d

    # --- supervisor controls ------------------------------------------------------------------

    def set_control(self, name: ControlName, on: bool, actor: str, now: datetime) -> None:
        """Kill switch / dry run. Audited first; raises AuditUnavailable if that fails."""
        self.audit.append(f"{name}_set", actor, now, detail={"on": on})
        self.controls.set(name, on, actor, now)
        if on:
            raise_alert(name, f"{name} switched on", actor=actor)
