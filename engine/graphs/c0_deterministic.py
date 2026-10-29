"""C0: deterministic pipeline. Rules, deterministic planner, gate, executor; an LLM is used at
most for explanations (if the context has one and `explain` is on)."""

from typing import Any

from engine.graphs.nodes import (
    DecisionContext,
    build_graph,
    deterministic_choice,
    deterministic_monitor,
)


def build(ctx: DecisionContext) -> Any:
    return build_graph(ctx, deterministic_monitor(ctx), deterministic_choice(ctx))
