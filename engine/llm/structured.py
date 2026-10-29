"""One way to ask an LLM a question: structured, validated, budgeted and logged.

`StructuredLLM.ask()` sends a system prompt and a user message, expects a JSON object, and
validates it with a Pydantic schema. It never raises for LLM trouble: an unavailable model,
an exhausted token budget or malformed output all come back as `parsed=None` with an error,
and the caller falls back to the deterministic path and marks the run degraded.

Every call (including refused and failed ones) is written to ops.LLMCalls with the model
name and version, prompt and completion tokens, latency, and the prompt and completion text.
Temperature is fixed at 0 by the chat-model factory.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, insert

from engine.data.schema import LLM_CALLS
from engine.observability import LLM_CALLS as LLM_CALL_COUNT
from engine.observability import LLM_TOKENS, log_event


@dataclass
class TokenBudget:
    """Shared by every LLM role in one run, so configurations get the same budget."""

    limit: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def charge(self, tokens: int) -> None:
        self.used += tokens


@dataclass(frozen=True)
class LLMUsage:
    model: str
    model_version: str | None
    prompt_tokens: int
    completion_tokens: int
    tokens_estimated: bool
    latency_ms: float


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    parsed: T | None
    raw: str = ""
    error: str | None = None
    usage: LLMUsage | None = None

    @property
    def ok(self) -> bool:
        return self.parsed is not None


def parse_json_object[T: BaseModel](raw: str, schema: type[T]) -> T:
    """Validate the first {...} block of `raw`. Raises ValueError/ValidationError if invalid."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the output")
    return schema.model_validate(json.loads(raw[start : end + 1]))


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part if isinstance(part, str) else str(part.get("text", "")) for part in content)


class LLMCallLog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def write(self, row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(LLM_CALLS), row)


@dataclass
class StructuredLLM:
    chat_model: BaseChatModel
    model_name: str
    budget: TokenBudget
    run_id: str
    log: LLMCallLog | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)  # this run's call records

    def _record(
        self,
        role: str,
        correlation_id: str,
        at: datetime,
        prompt: str,
        raw: str,
        error: str | None,
        usage: LLMUsage | None,
    ) -> None:
        row = {
            "RunID": self.run_id,
            "CorrelationID": correlation_id,
            "Role": role,
            "Model": usage.model if usage else self.model_name,
            "ModelVersion": usage.model_version if usage else None,
            "PromptTokens": usage.prompt_tokens if usage else 0,
            "CompletionTokens": usage.completion_tokens if usage else 0,
            "TokensEstimated": int(usage.tokens_estimated) if usage else 0,
            "LatencyMs": usage.latency_ms if usage else 0.0,
            "Ok": int(error is None),
            "Error": error,
            "Prompt": prompt,
            "Completion": raw,
            "At": at,
        }
        self.calls.append(row)
        LLM_CALL_COUNT.labels(
            role=role, result="ok" if error is None else error.split(":")[0]
        ).inc()
        if usage:
            LLM_TOKENS.labels(role=role).inc(usage.prompt_tokens + usage.completion_tokens)
        log_event(
            "llm",
            "llm call",
            correlation_id=correlation_id,
            role=role,
            model=row["Model"],
            ok=error is None,
            error=error,
            tokens=(usage.prompt_tokens + usage.completion_tokens) if usage else 0,
            latency_ms=row["LatencyMs"],
        )
        if self.log is not None:
            self.log.write(row)

    def ask[T: BaseModel](
        self,
        role: str,
        system: str,
        user: str,
        schema: type[T],
        *,
        correlation_id: str,
        at: datetime,
    ) -> LLMResult[T]:
        prompt = f"{system}\n\n{user}"
        if self.budget.exhausted:
            self._record(role, correlation_id, at, prompt, "", "token_budget_exhausted", None)
            return LLMResult(None, error="token_budget_exhausted")

        started = time.perf_counter()
        try:
            messages = [SystemMessage(content=system), HumanMessage(content=user)]
            message = self.chat_model.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - any model failure means: fall back
            error = f"llm_unavailable: {type(exc).__name__}"
            self._record(role, correlation_id, at, prompt, "", error, None)
            return LLMResult(None, error=error)
        latency = (time.perf_counter() - started) * 1000
        raw = _text(message)

        meta = getattr(message, "usage_metadata", None) or {}
        estimated = not meta
        prompt_tokens = int(meta.get("input_tokens", len(prompt) // 4))
        completion_tokens = int(meta.get("output_tokens", len(raw) // 4))
        response_meta = getattr(message, "response_metadata", {}) or {}
        usage = LLMUsage(
            model=str(
                response_meta.get("model_name") or response_meta.get("model") or self.model_name
            ),
            model_version=(
                response_meta.get("system_fingerprint") or response_meta.get("model_version")
            ),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tokens_estimated=estimated,
            latency_ms=round(latency, 1),
        )
        self.budget.charge(prompt_tokens + completion_tokens)

        try:
            parsed = parse_json_object(raw, schema)
        except (ValueError, ValidationError):
            self._record(role, correlation_id, at, prompt, raw, "malformed_output", usage)
            return LLMResult(None, raw, "malformed_output", usage)
        self._record(role, correlation_id, at, prompt, raw, None, usage)
        return LLMResult(parsed, raw, None, usage)
