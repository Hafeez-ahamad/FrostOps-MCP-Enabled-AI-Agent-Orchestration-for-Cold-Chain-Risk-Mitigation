"""Smoke-test the configured LLM backend with at most 5 calls (docs/engineering-rules.md rule 9).

python -m eval.llm_smoke

Asks each role once on a canned incident (monitor, planner, single agent, explainer: 4 calls)
and prints whether the answer validated, plus model, tokens and latency. It writes nothing to
eval/results/: four calls are a connectivity check, not a measurement.

Default backend is local Ollama (`ollama pull qwen2.5:7b`). Running it against a hosted
backend (AGENT_LLM=OPENAI) costs money: ask the project owner first.
"""

from datetime import datetime

from engine.agents.context import ReadingPoint
from engine.agents.explainer import explain
from engine.agents.monitor import triage
from engine.agents.planner import choose
from engine.agents.single_agent import decide
from engine.cargo import load_cargo_types
from engine.incidents.manager import Incident
from engine.llm.factory import build_structured_llm
from engine.planner.planner import DeterministicPlanner
from engine.risk.score import RiskScore
from engine.rules.engine import RuleHit, RuleInput
from engine.settings import load_settings
from engine.sop.retriever import LocalSopRetriever

MAX_CALLS = 5
T0 = datetime(2026, 3, 2, 14, 0)


def main() -> None:
    settings = load_settings()
    llm = build_structured_llm(settings, None, "llm-smoke")
    sop = LocalSopRetriever().clauses
    incident = Incident(
        incident_id="INC-SMOKE-01",
        shipment_id="S-SMOKE",
        depot_id="DEP-LB",
        cargo_type="FRESH",
        status="open",
        opened_at=T0,
        last_hit_at=T0,
        rule_ids=("R-TEMP-01",),
        hit_count=1,
        first_seq=9,
        last_seq=9,
        risk=RiskScore(3.5, 0.2, 0.5, 0.5, 0.7),
        latest_evidence=(
            RuleHit(
                "R-TEMP-01",
                "S-SMOKE",
                9,
                T0,
                "valid",
                "§1",
                "cold_chain_sop.md §1",
                {"temperature_c": 4.4},
                {"max_c": 4.0},
            ),
        ),
    )
    reading = RuleInput(
        "S-SMOKE", 9, T0, "DEP-LB", "FRESH", 4.4, None, "Low Risk", 0.1, 0.0, 33.9, -118.2
    )
    window = [
        ReadingPoint(T0.replace(minute=m), t, 0.0)
        for m, t in [(20, 2.0), (25, 2.1), (30, 2.0), (35, 3.1), (40, 4.4)]
    ]
    plan = DeterministicPlanner().plan(incident, reading, [])
    kwargs = {"correlation_id": "COR-SMOKE", "at": T0}

    results = {
        "monitor": triage(llm, incident, window, load_cargo_types()["FRESH"], **kwargs),
        "planner": choose(llm, plan, incident, [], sop, **kwargs),
        "single_agent": decide(llm, plan, incident, window, [], sop, **kwargs),
    }
    explanation, explainer_fell_back = explain(llm, incident, [], sop, **kwargs)
    results["explainer"] = None if explainer_fell_back else explanation
    assert len(llm.calls) <= MAX_CALLS
    for role, answer in results.items():
        print(f"{role:13s} {'valid' if answer is not None else 'NO VALID ANSWER'}: {answer}")
    for call in llm.calls:
        print(
            f"  {call['Role']:13s} model={call['Model']} tokens={call['PromptTokens']}+"
            f"{call['CompletionTokens']} latency={call['LatencyMs']} ms error={call['Error']}"
        )


if __name__ == "__main__":
    main()
