"""Switchable chat-model factory.

Clients are built only when `build_chat_model` is called, never at import time.
Temperature is always 0.
"""

from langchain_core.language_models import BaseChatModel
from sqlalchemy import Engine

from engine.llm.structured import LLMCallLog, StructuredLLM, TokenBudget
from engine.settings import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
    if settings.agent_llm == "OLLAMA":
        from langchain_ollama import ChatOllama

        model: BaseChatModel = ChatOllama(
            model=settings.ollama_model, temperature=0, seed=settings.llm_seed, num_predict=1024
        )
        return model
    if settings.agent_llm == "OPENAI":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=settings.openai_model, temperature=0)
    raise ValueError(f"Unknown LLM backend: {settings.agent_llm!r}")


def build_structured_llm(settings: Settings, engine: Engine | None, run_id: str) -> StructuredLLM:
    """The LLM the agent roles use: the configured backend, the run's token budget, and (if a
    database engine is given) a log row in ops.LLMCalls for every call."""
    model_name = settings.ollama_model if settings.agent_llm == "OLLAMA" else settings.openai_model
    return StructuredLLM(
        chat_model=build_chat_model(settings),
        model_name=model_name,
        budget=TokenBudget(settings.llm_token_budget_per_run),
        run_id=run_id,
        log=LLMCallLog(engine) if engine is not None else None,
    )
