"""Shared graph state, context and nodes for the C0/C1/C2 decision graphs.

One graph run handles one incident event (opened, or its set of rules changed):

    monitor -> plan -> choose -> submit -> explain

Hand-offs are persisted typed messages (engine/bus): the monitor publishes IncidentOpened, the
plan node consumes it; the submit node publishes PlanProposed (consumed by the gate) and
ActionApproved for anything the gate lets through.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from engine.actions.models import GateDecision, GateOutcome
from engine.agents.context import ReadingPoint
from engine.agents.explainer import Explanation, explain
from engine.agents.monitor import TriageVerdict
from engine.bus.messages import ActionApproved, IncidentOpened, PlanProposed, correlation_id
from engine.bus.queue import MessageBus
from engine.cargo import CargoType, load_cargo_types
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.llm.structured import StructuredLLM
from engine.planner.planner import Candidate, DeterministicPlanner, Plan
from engine.rules.engine import RuleInput
from engine.sop.retriever import LocalSopRetriever, SopClause

TRIAGE_MIN_CONFIDENCE = 0.7


class DecisionState(TypedDict, total=False):
    replan: bool
    incident: Incident
    reading: RuleInput
    window: list[ReadingPoint]
    facilities: list[FacilityStatus] | None
    weather_note: str  # set when the weather read was stale or failed (low confidence)
    now: datetime
    triage: TriageVerdict | None
    plan: Plan
    choice: int
    planner_kind: Literal["deterministic", "llm"]
    degraded: bool
    note: str
    verdict: str | None  # the LLM triage verdict (C1 single agent or C2 monitor), if any
    decisions: list[GateDecision]


@dataclass(frozen=True)
class ChoiceRecord:
    """One primary decision of a graph run, kept for evaluation (eval/metrics.py)."""

    incident_id: str
    shipment_id: str
    at: datetime
    replan: bool
    action: str
    target: str
    choice_index: int
    candidate_count: int
    top_action: str  # what the deterministic ranking would have chosen
    planner_kind: str
    degraded: bool
    close_call: bool
    verdict: str | None
    outcome: str  # gate outcome of the chosen candidate


@dataclass
class DecisionContext:
    planner: DeterministicPlanner
    gate: PolicyGate
    executor: Executor
    bus: MessageBus
    llm: StructuredLLM | None = None
    explain: bool = False
    sop: LocalSopRetriever = field(default_factory=LocalSopRetriever)
    cargo_types: dict[str, CargoType] = field(default_factory=load_cargo_types)
    degraded_steps: list[str] = field(default_factory=list)
    explanations: dict[str, Explanation] = field(default_factory=dict)
    choices: list[ChoiceRecord] = field(default_factory=list)

    def sop_for(self, incident: Incident) -> list[SopClause]:
        wanted = {h.sop_clause for h in incident.latest_evidence} or {"§1", "§2", "§3"}
        return [c for c in self.sop.clauses if c.clause_id in wanted]

    def mark_degraded(self, step: str, incident: Incident) -> None:
        self.degraded_steps.append(f"{step}:{incident.incident_id}")


class Node(Protocol):
    """A graph node: reads the state, returns the keys it updates."""

    def __call__(self, state: DecisionState) -> dict[str, Any]: ...


# --- monitor -----------------------------------------------------------------------------


def deterministic_monitor(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        incident = state["incident"]
        message = IncidentOpened(
            incident_id=incident.incident_id,
            shipment_id=incident.shipment_id,
            rule_ids=incident.rule_ids,
            risk_score=incident.risk.score,
            replan=state.get("replan", False),
        )
        ctx.bus.publish(correlation_id(incident.incident_id), "monitor", message, state["now"])
        return {"triage": None, "degraded": False}

    return node


# --- plan --------------------------------------------------------------------------------


def plan_node(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        incident = state["incident"]
        received = ctx.bus.consume(
            "planner", "IncidentOpened", correlation_id(incident.incident_id), state["now"]
        )
        assert received, "the planner only acts on a monitor message"
        body = received[-1].body
        assert isinstance(body, IncidentOpened)
        plan = ctx.planner.plan(incident, state["reading"], state.get("facilities"), body.triage)
        if note := state.get("weather_note"):
            plan = replace(plan, confidence="low", notes=(*plan.notes, note))
        return {"plan": plan}

    return node


def deterministic_choice(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        return {"choice": 0, "planner_kind": "deterministic"}

    return node


# --- submit ------------------------------------------------------------------------------


def _only_preconditions(d: GateDecision) -> bool:
    return d.outcome is GateOutcome.REJECTED and all(
        r.startswith("precondition:") for r in d.reasons
    )


def submit_candidate(
    ctx: DecisionContext,
    state: DecisionState,
    candidate: Candidate,
    planner_kind: Literal["deterministic", "llm"],
    degraded: bool,
    note: str = "",
) -> GateDecision:
    incident, now = state["incident"], state["now"]
    cor = correlation_id(incident.incident_id)
    proposal = ctx.planner.proposal(
        state["plan"],
        candidate,
        incident,
        state["reading"],
        now,
        planner=planner_kind,
        degraded=degraded,
        note=note,
    )
    ctx.bus.publish(
        cor,
        "planner",
        PlanProposed(
            incident_id=incident.incident_id,
            proposal_id=proposal.proposal_id,
            proposal_hash=proposal.content_hash(),
            action=proposal.action,
            target=proposal.target,
            planner=planner_kind,
            close_call=state["plan"].close_call,
            degraded=degraded,
        ),
        now,
    )
    ctx.bus.consume("policy_gate", "PlanProposed", cor, now)
    decision = ctx.gate.submit(proposal, now)
    if decision.outcome is GateOutcome.EXECUTE:
        ctx.bus.publish(
            cor,
            "policy_gate",
            ActionApproved(
                incident_id=incident.incident_id,
                proposal_id=proposal.proposal_id,
                action=proposal.action,
                level=decision.level.value if decision.level else "?",
                approved_by="policy_gate",
            ),
            now,
        )
        ctx.executor.enqueue(decision, now)
    return decision


def submit_node(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        plan = state["plan"]
        decisions: list[GateDecision] = []
        if plan.candidates:
            chosen = plan.candidates[state.get("choice", 0)]
            order = [chosen] + [c for c in plan.candidates if c != chosen]
            kind = state.get("planner_kind", "deterministic")
            degraded = state.get("degraded", False)
            for i, candidate in enumerate(order):
                decision = submit_candidate(
                    ctx,
                    state,
                    candidate,
                    kind if i == 0 else "deterministic",
                    degraded,
                    state.get("note", "") if i == 0 else "fallback after a failed precondition",
                )
                decisions.append(decision)
                if i == 0:
                    incident = state["incident"]
                    triage = state.get("triage")
                    ctx.choices.append(
                        ChoiceRecord(
                            incident_id=incident.incident_id,
                            shipment_id=incident.shipment_id,
                            at=state["now"],
                            replan=state.get("replan", False),
                            action=candidate.action,
                            target=candidate.target,
                            choice_index=state.get("choice", 0),
                            candidate_count=len(plan.candidates),
                            top_action=plan.candidates[0].action,
                            planner_kind=kind,
                            degraded=degraded,
                            close_call=plan.close_call,
                            verdict=state.get("verdict") or (triage.verdict if triage else None),
                            outcome=decision.outcome.value,
                        )
                    )
                if not _only_preconditions(decision):
                    break
        for companion in plan.companions:
            decisions.append(submit_candidate(ctx, state, companion, "deterministic", False))
        return {"decisions": decisions}

    return node


# --- explain -----------------------------------------------------------------------------


def explain_node(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        if not ctx.explain:
            return {}
        incident = state["incident"]
        explanation, degraded = explain(
            ctx.llm,
            incident,
            state.get("decisions", []),
            ctx.sop_for(incident),
            correlation_id=correlation_id(incident.incident_id),
            at=state["now"],
        )
        if degraded:
            ctx.mark_degraded("explainer", incident)
        ctx.explanations[incident.incident_id] = explanation
        return {}

    return node


def build_graph(ctx: DecisionContext, monitor: Node, choose: Node) -> Any:
    builder = StateGraph(DecisionState)
    builder.add_node("monitor", monitor)
    builder.add_node("plan", plan_node(ctx))
    builder.add_node("choose", choose)
    builder.add_node("submit", submit_node(ctx))
    builder.add_node("explain", explain_node(ctx))
    builder.add_edge(START, "monitor")
    builder.add_edge("monitor", "plan")
    builder.add_edge("plan", "choose")
    builder.add_edge("choose", "submit")
    builder.add_edge("submit", "explain")
    builder.add_edge("explain", END)
    return builder.compile()


def accept_triage(verdict: TriageVerdict | None) -> str | None:
    """Only a confident verdict changes the plan."""
    if verdict is None or verdict.confidence < TRIAGE_MIN_CONFIDENCE:
        return None
    return verdict.verdict
