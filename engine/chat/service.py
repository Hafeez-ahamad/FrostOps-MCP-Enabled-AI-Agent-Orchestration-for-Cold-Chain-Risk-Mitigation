"""The single constructor every chat entry point (CLI, dispatch API) uses."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import Engine

from engine.chat.graph import build_chat_graph
from engine.chat.tools import build_chat_tools
from engine.llm.factory import build_chat_model
from engine.prompts import load_prompt
from engine.settings import Settings, load_settings

CHAT_PROMPT_NAME = "chat_system"


def durable_checkpointer(path: Path) -> SqliteSaver:
    """Conversation state in a SQLite file, so a chat survives a restart of the process.
    Streamlit serves sessions from several threads, hence check_same_thread=False."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def create_chat_agent(
    settings: Settings | None = None,
    llm: BaseChatModel | None = None,
    engine_factory: Callable[[], Engine] | None = None,
) -> Any:
    """`engine_factory` overrides the read-only explainer login (e.g. the API's database)."""
    settings = settings or load_settings()
    return build_chat_graph(
        llm=llm or build_chat_model(settings),
        tools=build_chat_tools(settings, engine_factory=engine_factory),
        system_prompt=load_prompt(CHAT_PROMPT_NAME),
        checkpointer=durable_checkpointer(settings.chat_checkpoint_path),
    )
