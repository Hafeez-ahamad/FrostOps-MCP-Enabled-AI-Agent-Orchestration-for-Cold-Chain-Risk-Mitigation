"""Turn a graph run into simple events that the CLI and the console both display."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


@dataclass(frozen=True)
class ChatEvent:
    kind: Literal["tool_call", "tool_result", "answer"]
    name: str
    content: str


def run_chat_turn(agent: Any, user_text: str, thread_id: str) -> Iterator[ChatEvent]:
    config = {"configurable": {"thread_id": thread_id}}
    updates = agent.stream(
        {"messages": [HumanMessage(content=user_text)]}, config=config, stream_mode="updates"
    )
    for update in updates:
        for node_state in update.values():
            for msg in node_state.get("messages", []):
                if isinstance(msg, AIMessage):
                    for call in msg.tool_calls:
                        yield ChatEvent("tool_call", call["name"], json.dumps(call["args"]))
                    if msg.content:
                        yield ChatEvent("answer", "assistant", str(msg.content))
                elif isinstance(msg, ToolMessage):
                    yield ChatEvent("tool_result", msg.name or "tool", str(msg.content))
