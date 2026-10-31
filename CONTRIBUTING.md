# Contributing

## Setup

Python 3.12. On Windows use `.\tasks.ps1 setup`. Elsewhere:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

## Checks

All four must pass before a change is merged. The default test run needs no Docker, network or
API keys.

```bash
python -m pytest
ruff check .
ruff format --check .
mypy engine
```

Tests marked `integration` need SQL Server 2022 and ODBC Driver 18 (`pytest -m integration`);
CI runs them.

## Changes

- Keep commits small, with a plain message that says what changed.
- Add a boundary test first for any threshold or permission (for example 4.0 vs 4.01).
- Never weaken a test to make it pass. If a test is wrong, fix it and note why in
  `docs/decisions.md`.
- Any number in the docs must come from `eval/results/`; otherwise write
  `TBD (not yet measured)`.
