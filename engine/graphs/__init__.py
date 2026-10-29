"""The three compared configurations (spec section 13.1) as LangGraph graphs.

All three share the same deterministic detection, planner candidates, policy gate, executor,
tools and token budget. They differ only in where an LLM is consulted:

  C0  deterministic: no LLM in the decision (optionally the explainer)
  C1  single agent:  one LLM call triages and chooses among the candidates
  C2  multi-agent:   monitor LLM triages temperature incidents; planner LLM only on close calls

Every LLM step falls back to the deterministic answer when the LLM gives no valid answer, and
the decision is marked degraded.
"""

from engine.graphs.c0_deterministic import build as build_c0
from engine.graphs.c1_single import build as build_c1
from engine.graphs.c2_multi import build as build_c2
from engine.graphs.nodes import DecisionContext, DecisionState, build_graph

CONFIGURATIONS = {"C0": build_c0, "C1": build_c1, "C2": build_c2}

__all__ = ["CONFIGURATIONS", "DecisionContext", "DecisionState", "build_graph"]
