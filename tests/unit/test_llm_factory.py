import pytest

from engine.llm.factory import build_chat_model
from engine.settings import Settings


def test_default_backend_is_local_ollama_at_temperature_zero(settings: Settings) -> None:
    model = build_chat_model(settings)
    assert type(model).__name__ == "ChatOllama"
    assert model.temperature == 0  # type: ignore[attr-defined]


def test_openai_backend_builds_without_network(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    model = build_chat_model(settings.model_copy(update={"agent_llm": "OPENAI"}))
    assert type(model).__name__ == "ChatOpenAI"
    assert model.temperature == 0  # type: ignore[attr-defined]


def test_unknown_backend_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError):
        build_chat_model(settings.model_copy(update={"agent_llm": "DEEPSEEK"}))


def test_backend_name_is_case_insensitive() -> None:
    assert Settings(_env_file=None, agent_llm="openai").agent_llm == "OPENAI"  # type: ignore[call-arg]
