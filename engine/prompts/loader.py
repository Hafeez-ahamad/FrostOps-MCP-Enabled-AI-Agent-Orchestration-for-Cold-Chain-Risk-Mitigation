"""The one place prompts are read from disk.

Every entry point gets its prompt through `load_prompt`. A missing or empty prompt is an
error; there is no silent fallback prompt.
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.txt"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Prompt file is empty: {path}")
    return text
