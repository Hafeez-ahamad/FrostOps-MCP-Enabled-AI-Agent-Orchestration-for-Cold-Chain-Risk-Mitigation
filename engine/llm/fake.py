"""A chat model whose replies come from a Python function. For tests and offline plumbing runs
only: it shows the configurations work end to end, and says nothing about LLM quality."""

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

Responder = Callable[[str, str], str]  # (system prompt, user message) -> reply text


class ResponderChatModel(BaseChatModel):
    responder: Responder
    model_label: str = "scripted-fake"

    @property
    def _llm_type(self) -> str:
        return "responder-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system = str(messages[0].content) if messages else ""
        user = str(messages[-1].content) if messages else ""
        reply = self.responder(system, user)
        message = AIMessage(
            content=reply,
            usage_metadata={
                "input_tokens": (len(system) + len(user)) // 4,
                "output_tokens": len(reply) // 4,
                "total_tokens": (len(system) + len(user) + len(reply)) // 4,
            },
            response_metadata={"model_name": self.model_label},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ResponderChatModel":
        return self


_TEMPS = re.compile(r"^\d\d:\d\d \| (-?[\d.]+) \|", re.MULTILINE)
_CANDIDATE = re.compile(r"^\[(\d+)\] (\w+) target=", re.MULTILINE)
_RULES = re.compile(r"rules ([A-Z0-9, -]+);")


def heuristic_responder(system: str, user: str) -> str:
    """Scripted replies that read the prompt the way a careful model might: a single-step jump
    of 1 C or more is a door-open event. For plumbing tests only, never for reported results."""
    temps = [float(t) for t in _TEMPS.findall(user)]
    jump = max((b - a for a, b in zip(temps, temps[1:], strict=False)), default=0.0)
    if jump >= 1.0:
        verdict = "door_open_event"
    else:
        verdict = "real_breach" if len(temps) >= 3 else "uncertain"
    if "MONITOR agent" in system:
        return json.dumps({"verdict": verdict, "confidence": 0.9, "reasons": f"step {jump:.1f}"})
    if "dispatch agent" in system:
        choice = 0
        if verdict == "door_open_event":
            for index, action in _CANDIDATE.findall(user):
                if action == "create_incident_ticket":
                    choice = int(index)
        return json.dumps({"verdict": verdict, "confidence": 0.9, "choice": choice, "reasons": "x"})
    if "PLANNER agent" in system:
        return json.dumps({"choice": 0, "reasons": "SOP"})
    rules = _RULES.search(user)
    cited = [r.strip() for r in rules.group(1).split(",")] if rules else []
    return json.dumps({"summary": "ok", "rule_ids": cited, "sop_clauses": []})
