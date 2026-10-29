"""C1: single agent. One LLM call sees everything (incident, telemetry, candidates, facilities,
SOP) and returns a triage verdict and a choice among the deterministic candidates. It is asked
whenever there is more than one candidate. Same gate and executor as C0 and C2."""

from typing import Any

from engine.agents.single_agent import decide
from engine.bus.messages import correlation_id
from engine.graphs.nodes import (
    DecisionContext,
    DecisionState,
    Node,
    build_graph,
    deterministic_monitor,
)


def single_agent_choice(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        plan, incident = state["plan"], state["incident"]
        if len(plan.candidates) < 2:
            return {"choice": 0, "planner_kind": "deterministic"}
        answer = decide(
            ctx.llm,
            plan,
            incident,
            state.get("window", []),
            state.get("facilities"),
            ctx.sop_for(incident),
            correlation_id=correlation_id(incident.incident_id),
            at=state["now"],
        )
        if answer is None:
            ctx.mark_degraded("single_agent", incident)
            return {"choice": 0, "planner_kind": "deterministic", "degraded": True}
        return {
            "choice": answer.choice,
            "planner_kind": "llm",
            "verdict": answer.verdict,
            "note": f"Single agent: {answer.verdict} ({answer.confidence:.2f}); {answer.reasons}",
        }

    return node


def build(ctx: DecisionContext) -> Any:
    return build_graph(ctx, deterministic_monitor(ctx), single_agent_choice(ctx))
