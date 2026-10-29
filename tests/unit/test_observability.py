"""Structured JSON logs with correlation ids, Prometheus metrics and alerts (spec section 14)."""

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel

from engine.llm.fake import ResponderChatModel
from engine.llm.structured import StructuredLLM, TokenBudget
from engine.observability import REGISTRY, JsonFormatter, metrics_text
from tests.unit.harness import T0, Harness


@contextmanager
def captured() -> Iterator[list[dict[str, Any]]]:
    """JSON lines the engine logs while the block runs."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("coldchain")
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    lines: list[dict[str, Any]] = []
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
        lines.extend(json.loads(line) for line in stream.getvalue().splitlines())


def test_every_log_line_is_json_and_executor_lines_carry_the_correlation_id() -> None:
    with captured() as lines:
        h = Harness.build()
        h.submit_and_enqueue(h.proposal("create_incident_ticket"))
        h.executor.run_once(T0)
    assert lines and all({"ts", "level", "logger", "event"} <= set(line) for line in lines)
    gate = [line for line in lines if line["logger"] == "coldchain.gate"]
    assert gate and gate[0]["fields"]["outcome"] == "execute"
    executed = [line for line in lines if line["event"] == "action_succeeded"]
    assert executed and executed[0]["correlation_id"] == "COR-INC-S1-01"
    assert executed[0]["fields"]["action"] == "create_incident_ticket"


def test_an_alert_is_a_warning_line_and_a_counter() -> None:
    before = REGISTRY.get_sample_value("coldchain_alerts_total", {"kind": "kill_switch"}) or 0.0
    with captured() as lines:
        h = Harness.build()
        h.gate.set_control("kill_switch", True, "supervisor:bob", T0)
    (line,) = [x for x in lines if x["logger"] == "coldchain.alerts"]
    assert line["level"] == "WARNING" and line["fields"]["kind"] == "kill_switch"
    after = REGISTRY.get_sample_value("coldchain_alerts_total", {"kind": "kill_switch"})
    assert after == before + 1


class Answer(BaseModel):
    ok: bool


def test_llm_calls_and_tokens_are_counted_per_role() -> None:
    labels = {"role": "observability_test", "result": "ok"}
    before = REGISTRY.get_sample_value("coldchain_llm_calls_total", labels) or 0.0
    llm = StructuredLLM(
        ResponderChatModel(responder=lambda s, u: '{"ok": true}'), "fake", TokenBudget(10**6), "r"
    )
    result = llm.ask("observability_test", "sys", "user", Answer, correlation_id="COR-X", at=T0)
    assert result.ok
    assert REGISTRY.get_sample_value("coldchain_llm_calls_total", labels) == before + 1
    tokens = REGISTRY.get_sample_value("coldchain_llm_tokens_total", {"role": "observability_test"})
    assert tokens is not None and tokens > 0


def test_metrics_text_has_every_counter_and_gauge() -> None:
    text = metrics_text().decode()
    for name in (
        "coldchain_incidents_opened_total",
        "coldchain_gate_decisions_total",
        "coldchain_actions_total",
        "coldchain_human_decisions_total",
        "coldchain_llm_calls_total",
        "coldchain_llm_tokens_total",
        "coldchain_alerts_total",
        "coldchain_approval_queue_depth",
        "coldchain_kill_switch",
        "coldchain_alert_only_mode",
    ):
        assert f"# TYPE {name.removesuffix('_total')}" in text, name
