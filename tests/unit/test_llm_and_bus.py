"""Structured LLM calls (validation, fallback, budget, logging) and the typed message bus."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy import select

from engine.bus.messages import (
    ActionExecuted,
    IncidentOpened,
    Message,
    PlanProposed,
    correlation_id,
)
from engine.bus.queue import MessageBus
from engine.data.memory_db import in_memory_engine
from engine.data.schema import LLM_CALLS
from engine.llm.fake import ResponderChatModel
from engine.llm.structured import LLMCallLog, StructuredLLM, TokenBudget, parse_json_object

T0 = datetime(2026, 3, 2, 12, 0)


class Answer(BaseModel):
    choice: int
    reasons: str = ""


def llm(reply: str, budget: int = 10_000, engine: Any = None) -> StructuredLLM:
    return StructuredLLM(
        chat_model=ResponderChatModel(responder=lambda system, user: reply),
        model_name="fake",
        budget=TokenBudget(budget),
        run_id="run-1",
        log=LLMCallLog(engine) if engine is not None else None,
    )


def ask(model: StructuredLLM) -> Any:
    return model.ask("planner", "system", "user", Answer, correlation_id="COR-1", at=T0)


def test_valid_json_is_parsed_even_with_surrounding_text() -> None:
    result = ask(llm('Sure! {"choice": 2, "reasons": "SOP §1"} Hope that helps.'))
    assert result.ok and result.parsed.choice == 2
    assert result.usage is not None and result.usage.model == "scripted-fake"


@pytest.mark.parametrize(
    "reply", ["no json here", '{"choice": "two"}', '{"reasons": "x"}', "{not json}"]
)
def test_malformed_output_gives_no_answer(reply: str) -> None:
    result = ask(llm(reply))
    assert not result.ok and result.error == "malformed_output"


def test_unavailable_model_gives_no_answer() -> None:
    class Broken(ResponderChatModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("ollama is not running")

    model = StructuredLLM(Broken(responder=lambda s, u: ""), "fake", TokenBudget(1000), "run-1")
    result = ask(model)
    assert not result.ok and result.error == "llm_unavailable: ConnectionError"


def test_budget_is_charged_and_then_refuses() -> None:
    model = llm('{"choice": 0}', budget=3)
    first = ask(model)
    assert first.ok and model.budget.used > 3  # the call that crosses the limit still completes
    second = ask(model)
    assert not second.ok and second.error == "token_budget_exhausted"


def test_every_call_is_logged_with_tokens_latency_and_text() -> None:
    engine = in_memory_engine()
    model = llm("garbage", engine=engine)
    ask(model)
    model.budget.used = model.budget.limit
    ask(model)
    with engine.connect() as conn:
        rows = conn.execute(select(LLM_CALLS).order_by(LLM_CALLS.c.CallID)).mappings().all()
    assert [r["Error"] for r in rows] == ["malformed_output", "token_budget_exhausted"]
    assert rows[0]["Role"] == "planner" and rows[0]["CorrelationID"] == "COR-1"
    assert rows[0]["PromptTokens"] > 0 and rows[0]["Completion"] == "garbage"
    assert rows[0]["Model"] == "scripted-fake" and rows[0]["LatencyMs"] >= 0


def test_parse_rejects_text_without_an_object() -> None:
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]", Answer)


def test_factory_builds_the_llm_lazily(settings) -> None:  # type: ignore[no-untyped-def]
    from engine.llm.factory import build_structured_llm

    model = build_structured_llm(settings, None, "run-x")
    assert isinstance(model.chat_model, BaseChatModel)
    assert model.budget.limit == settings.llm_token_budget_per_run


# --- bus ---------------------------------------------------------------------------------


def opened(incident: str = "INC-S1-01") -> IncidentOpened:
    return IncidentOpened(
        incident_id=incident, shipment_id="S1", rule_ids=("R-TEMP-01",), risk_score=3.5
    )


def test_publish_and_consume_in_order_once() -> None:
    bus = MessageBus(in_memory_engine())
    cor = correlation_id("INC-S1-01")
    bus.publish(cor, "monitor", opened(), T0)
    bus.publish(
        cor, "monitor", opened().model_copy(update={"replan": True}), T0 + timedelta(minutes=5)
    )
    got = bus.consume("planner", "IncidentOpened", cor, T0)
    assert [m.body.replan for m in got] == [False, True]  # type: ignore[union-attr]
    assert bus.consume("planner", "IncidentOpened", cor, T0) == []


def test_messages_are_scoped_by_correlation_id_and_type() -> None:
    bus = MessageBus(in_memory_engine())
    bus.publish(correlation_id("A"), "monitor", opened("A"), T0)
    bus.publish(correlation_id("B"), "monitor", opened("B"), T0)
    bus.publish(
        correlation_id("A"),
        "executor",
        ActionExecuted(incident_id="A", action_id="ACT-1", action="x", target="S1", attempts=1),
        T0,
    )
    assert len(bus.consume("planner", "IncidentOpened", correlation_id("A"), T0)) == 1
    history = bus.history(correlation_id("A"))
    assert [m.body.type for m in history] == ["IncidentOpened", "ActionExecuted"]
    assert history[0].producer == "monitor"


def test_message_payloads_are_validated() -> None:
    adapter: TypeAdapter[Message] = TypeAdapter(Message)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "PlanProposed", "incident_id": "A"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "DeleteEverything"})
    ok = adapter.validate_python(
        {
            "type": "PlanProposed",
            "incident_id": "A",
            "proposal_id": "P",
            "proposal_hash": "h",
            "action": "x",
            "target": "S1",
            "planner": "llm",
            "close_call": False,
            "degraded": False,
        }
    )
    assert isinstance(ok, PlanProposed)
