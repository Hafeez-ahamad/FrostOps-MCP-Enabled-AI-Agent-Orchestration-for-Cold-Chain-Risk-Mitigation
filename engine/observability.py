"""Structured JSON logs with correlation ids, and Prometheus metrics (spec section 14).

Every log line is one JSON object: time, level, logger, event, correlation id (if any) and
fields. Metrics use a dedicated registry so tests and the API can read them independently of
the process-global default registry.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

INCIDENTS_OPENED = Counter(
    "coldchain_incidents_opened_total", "Incidents opened", registry=REGISTRY
)
GATE_DECISIONS = Counter(
    "coldchain_gate_decisions_total",
    "Gate decisions by outcome and action",
    ["outcome", "action"],
    registry=REGISTRY,
)
ACTIONS = Counter(
    "coldchain_actions_total",
    "Executor outcomes by status and action",
    ["status", "action"],
    registry=REGISTRY,
)
APPROVALS = Counter(
    "coldchain_human_decisions_total",
    "Human approvals and rejections",
    ["decision"],
    registry=REGISTRY,
)
LLM_CALLS = Counter(
    "coldchain_llm_calls_total",
    "LLM calls by role and result",
    ["role", "result"],
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "coldchain_llm_tokens_total", "LLM tokens (prompt + completion)", ["role"], registry=REGISTRY
)
TOOL_LATENCY = Histogram(
    "coldchain_tool_latency_seconds", "Integration call latency", ["tool"], registry=REGISTRY
)
ALERTS = Counter("coldchain_alerts_total", "Operational alerts raised", ["kind"], registry=REGISTRY)
QUEUE_DEPTH = Gauge(
    "coldchain_approval_queue_depth", "Proposals awaiting a human", registry=REGISTRY
)
KILL_SWITCH = Gauge("coldchain_kill_switch", "1 when the kill switch is on", registry=REGISTRY)
ALERT_ONLY = Gauge(
    "coldchain_alert_only_mode", "1 when the engine is in alert-only mode", registry=REGISTRY
)


def metrics_text() -> bytes:
    return generate_latest(REGISTRY)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in ("correlation_id", "fields"):
            value = getattr(record, key, None)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    """Send the engine's logs to stderr as JSON lines (idempotent)."""
    root = logging.getLogger("coldchain")
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)


def log_event(
    logger: str,
    event: str,
    *,
    correlation_id: str | None = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    logging.getLogger(f"coldchain.{logger}").log(
        level, event, extra={"correlation_id": correlation_id, "fields": fields or None}
    )


def raise_alert(
    kind: str, message: str, *, correlation_id: str | None = None, **fields: Any
) -> None:
    """An operational alert: a WARNING log line plus a metric (spec section 14 alerts)."""
    ALERTS.labels(kind=kind).inc()
    log_event(
        "alerts", message, correlation_id=correlation_id, level=logging.WARNING, kind=kind, **fields
    )
