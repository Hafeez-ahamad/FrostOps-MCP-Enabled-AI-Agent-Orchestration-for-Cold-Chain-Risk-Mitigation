"""C3 (red team only): configuration C2 with the policy gate removed.

Every proposal is stored as approved and executed directly: no preconditions, limits, kill
switch or human approval. For each proposal the real gate's verdict is also computed in
"shadow" mode (nothing stored), so the run can count the actions the gate would have refused.
This shows what the gate prevents. It must never be deployed; it exists only for the replay.
"""

from datetime import datetime

from engine.actions.models import GateDecision, GateOutcome, Proposal
from engine.actions.policy import ActionLevel
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from engine.runtime import Runtime

ACTOR = "no_gate(red_team)"


class NoGate(PolicyGate):
    """Approves everything. Keeps a shadow record of what the real gate would have decided."""

    shadow: dict[str, GateOutcome]

    def submit(self, p: Proposal, now: datetime) -> GateDecision:
        if not hasattr(self, "shadow"):
            self.shadow = {}
        if self.proposals.get(p.proposal_id) is not None:
            return self._decision(p, GateOutcome.DUPLICATE, ["already decided"], now, None)
        policy = self.catalog.get(p.action)
        if policy is None:
            would = GateOutcome.REJECTED
        elif policy.level in (ActionLevel.L0, ActionLevel.L1, ActionLevel.L4):
            would = GateOutcome.REJECTED
        elif self._limits(p, policy, now) or self._precondition_failures(p, policy):
            would = GateOutcome.REJECTED
        elif policy.level is ActionLevel.L3:
            would = GateOutcome.AWAIT_APPROVAL
        else:
            would = GateOutcome.EXECUTE
        self.shadow[p.proposal_id] = would
        level = policy.level if policy else ActionLevel.L4
        decision = self._decision(p, GateOutcome.EXECUTE, ["no_gate"], now, level)
        self.audit.append(
            "gate_bypassed",
            ACTOR,
            now,
            proposal_id=p.proposal_id,
            detail={"would_have_been": would.value, "action": p.action},
        )
        self.proposals.insert(p, level, "approved", now)
        return decision


class UnguardedExecutor(Executor):
    """Runs whatever it is given (no authorisation re-check)."""

    def _authorised(self, proposal_id: str, proposal_hash: str) -> tuple[bool, str]:
        return True, ""


def remove_gate(runtime: Runtime) -> NoGate:
    """Turn a built C2 runtime into C3 by swapping in the no-gate and the unguarded executor."""
    old_gate, old_exec = runtime.gate, runtime.executor
    no_gate = NoGate(runtime.engine, old_gate.catalog, runtime.integrations)
    no_gate.shadow = {}
    executor = UnguardedExecutor(runtime.engine, old_exec.catalog, runtime.integrations)
    runtime.gate = no_gate
    runtime.executor = executor
    runtime.dispatcher.ctx.gate = no_gate
    runtime.dispatcher.ctx.executor = executor
    return no_gate
