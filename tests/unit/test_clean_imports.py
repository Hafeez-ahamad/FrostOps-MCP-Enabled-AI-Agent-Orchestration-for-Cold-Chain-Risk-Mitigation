"""Engine modules import with no API keys, no .env, no network and no heavy ML stack.

A module that needs a key or loads an embedding model on import cannot be unit-tested, so
clients and models are built lazily.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODULES = [
    "engine.settings",
    "engine.db",
    "engine.llm.factory",
    "engine.prompts",
    "engine.sop.retriever",
    "engine.tools.telemetry",
    "engine.tools.corridor",
    "engine.chat.tools",
    "engine.chat.graph",
    "engine.chat.service",
    "engine.chat.stream",
]

# Must not be imported as a side effect of importing the engine.
FORBIDDEN = [
    "torch",
    "sentence_transformers",
    "pinecone",
    "langchain_pinecone",
    "langchain_huggingface",
    "streamlit",
    "langchain_openai",
    "langchain_ollama",
]

SCRIPT = f"""
import sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
import importlib
for name in {MODULES!r}:
    importlib.import_module(name)
from engine.chat.tools import build_chat_tools
from engine.settings import Settings
build_chat_tools(Settings(_env_file=None))
loaded = [m for m in {FORBIDDEN!r} if m in sys.modules]
print("LOADED:" + ",".join(loaded))
"""


def test_engine_imports_without_keys_or_heavy_modules(tmp_path: Path) -> None:
    secret_markers = ("KEY", "PASSWORD", "TOKEN", "SECRET")
    env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in secret_markers)}
    result = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT],
        cwd=tmp_path,  # no .env here
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("LOADED:"), result.stdout
