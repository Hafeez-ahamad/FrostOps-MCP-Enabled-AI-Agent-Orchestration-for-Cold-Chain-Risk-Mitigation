"""C2: multi-agent. Separate roles with separate inputs (least privilege):

- monitor LLM: sees the incident and recent temperatures only; triages temperature incidents;
  its verdict travels to the planner in the IncidentOpened message
- planner LLM: sees candidates, facilities and the SOP (no raw telemetry); asked only when the
  deterministic candidates are a close call
- explainer LLM (optional): read-only summary
The gate and executor are the same deterministic code as in C0 and C1."""

from typing import Any

from engine.agents.monitor import triage
from engine.agents.planner import choose
from engine.bus.messages import IncidentOpened, correlation_id
from engine.graphs.nodes import (
    DecisionContext,
    DecisionState,
    Node,
    accept_triage,
    build_graph,
)


def llm_monitor(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        incident, now = state["incident"], state["now"]
        verdict = None
        degraded = False
        if "R-TEMP-01" in incident.rule_ids:
            band = ctx.cargo_types.get(incident.cargo_type or "")
            verdict = triage(
                ctx.llm,
                incident,
                state.get("window", []),
                band,
                correlation_id=correlation_id(incident.incident_id),
                at=now,
            )
            if verdict is None:
                degraded = True
                ctx.mark_degraded("monitor", incident)
        message = IncidentOpened(
            incident_id=incident.incident_id,
            shipment_id=incident.shipment_id,
            rule_ids=incident.rule_ids,
            risk_score=incident.risk.score,
            triage=accept_triage(verdict),
            triage_degraded=degraded,
            replan=state.get("replan", False),
        )
        ctx.bus.publish(correlation_id(incident.incident_id), "monitor", message, now)
        return {"triage": verdict, "degraded": degraded}

    return node


def planner_choice(ctx: DecisionContext) -> Node:
    def node(state: DecisionState) -> dict[str, Any]:
        plan, incident = state["plan"], state["incident"]
        if not plan.close_call:
            return {"choice": 0, "planner_kind": "deterministic"}
        answer = choose(
            ctx.llm,
            plan,
            incident,
            state.get("facilities"),
            ctx.sop_for(incident),
            correlation_id=correlation_id(incident.incident_id),
            at=state["now"],
        )
        if answer is None:
            ctx.mark_degraded("planner", incident)
            return {"choice": 0, "planner_kind": "deterministic", "degraded": True}
        return {
            "choice": answer.choice,
            "planner_kind": "llm",
            "note": f"Planner agent: {answer.reasons}",
        }

    return node


def build(ctx: DecisionContext) -> Any:
    return build_graph(ctx, llm_monitor(ctx), planner_choice(ctx))
