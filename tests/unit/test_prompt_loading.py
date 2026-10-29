"""Regression: the system prompt reaches the model from every entry point.

The console, the CLI and the API must all send the same system prompt.
"""

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from engine.chat.graph import build_chat_graph
from engine.chat.service import CHAT_PROMPT_NAME, create_chat_agent
from engine.chat.stream import run_chat_turn
from engine.chat.tools import build_chat_tools
from engine.prompts import load_prompt
from engine.prompts.loader import PROMPTS_DIR
from tests.fakes import ScriptedChatModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The console is an API client now; the API builds the chat agent for it.
ENTRY_POINTS = [PROJECT_ROOT / "engine" / "chat" / "cli.py", PROJECT_ROOT / "api" / "__main__.py"]


def test_chat_prompt_loads_and_is_not_empty() -> None:
    assert len(load_prompt(CHAT_PROMPT_NAME)) > 200


def test_missing_prompt_raises_instead_of_falling_back() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("does_not_exist")


def test_empty_prompt_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "blank.txt").write_text("  \n", encoding="utf-8")
    monkeypatch.setattr("engine.prompts.loader.PROMPTS_DIR", tmp_path)
    with pytest.raises(ValueError):
        load_prompt("blank")


def test_system_prompt_sent_on_every_turn_exactly_once(settings) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedChatModel(replies=[AIMessage(content="first"), AIMessage(content="second")])
    agent = build_chat_graph(llm, build_chat_tools(settings), system_prompt="SYSTEM-XYZ")

    for text in ("hello", "and again"):
        list(run_chat_turn(agent, text, thread_id="t1"))

    assert len(llm.calls) == 2
    for call in llm.calls:
        system_messages = [m for m in call if isinstance(m, SystemMessage)]
        assert len(system_messages) == 1
        assert call[0] is system_messages[0]
        assert call[0].content == "SYSTEM-XYZ"


def test_service_agent_uses_the_prompt_file(settings) -> None:  # type: ignore[no-untyped-def]
    llm = ScriptedChatModel(replies=[AIMessage(content="answer")])
    agent = create_chat_agent(settings, llm=llm)

    events = list(run_chat_turn(agent, "hi", thread_id="t2"))

    assert events[-1].content == "answer"
    assert llm.calls[0][0].content == load_prompt(CHAT_PROMPT_NAME)


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_points_build_the_agent_through_the_service(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "create_chat_agent" in source
    assert "SystemMessage" not in source
    assert "load_prompt" not in source


def test_prompt_names_exactly_the_tools_that_exist(settings) -> None:  # type: ignore[no-untyped-def]
    prompt = load_prompt(CHAT_PROMPT_NAME)
    for tool in build_chat_tools(settings):
        assert tool.name in prompt
    for removed in ("query_telemetry_db", "fetch_corridor_conditions", "Pinecone"):
        assert removed not in prompt


def test_only_the_loader_lives_next_to_prompt_files() -> None:
    assert {p.suffix for p in PROMPTS_DIR.iterdir() if p.is_file()} <= {".py", ".txt"}


def test_chat_memory_survives_a_restart(settings) -> None:  # type: ignore[no-untyped-def]
    """Durable checkpointer: a new agent (as after a process restart) sees the old thread."""
    first = create_chat_agent(settings, llm=ScriptedChatModel(replies=[AIMessage(content="one")]))
    list(run_chat_turn(first, "remember shipment S1", thread_id="t-durable"))

    llm = ScriptedChatModel(replies=[AIMessage(content="two")])
    second = create_chat_agent(settings, llm=llm)
    list(run_chat_turn(second, "what did I ask?", thread_id="t-durable"))

    contents = [m.content for m in llm.calls[0]]
    assert "remember shipment S1" in contents
    assert settings.chat_checkpoint_path.exists()
