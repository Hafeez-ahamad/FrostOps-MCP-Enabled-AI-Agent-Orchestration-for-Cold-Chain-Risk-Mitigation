# Engineering rules

Policy-gated, multi-agent cold-chain dispatch engine on a **simulated** fleet.

- Source of truth: `docs/SPEC.md`. If a request and the spec disagree, the spec wins.
- Progress: `docs/PROGRESS.md` (an item is ticked only when its proof exists).
- Decisions where the spec is silent: `docs/decisions.md` (one line: decision, reason, date).

## Non-negotiable rules

1. No LLM decides a threshold, a permission or whether an action runs. SOP rules, data-quality
   checks, the policy gate and the executor are deterministic code with no LLM import. LLMs only
   do ambiguity triage, close-call planning, explanations and chat, always via Pydantic-validated output.
2. Default deny. Unknown actions are rejected. L3 needs a recorded human identity bound to the
   exact proposal hash; reused/old approvals are rejected. L4 never executes.
3. Fail closed. Audit write fails -> action does not run. Data quality not `valid` -> no L2 action.
   Dependency down -> degrade to a safer mode, never a more permissive one.
4. Never fabricate results. Every number in README/EVAL/docs comes from `eval/results/` produced by
   a committed script; otherwise write `TBD (not yet measured)`. Label outcomes **simulated**.
5. Honest language: "policy-bounded autonomy on a simulated fleet". No production/customer claims.
6. No secrets in the repo. Env vars (`.env`, gitignored) or Docker secrets; `.env.example` has
   placeholders only.
7. Tests are part of the work. Boundary tests first (4.0 vs 4.01, 7.0 vs 7.01, 0.65 vs 0.66).
8. No silent scope expansion: minimum viable cut (spec section 17) first. Terraform, Grafana,
   React console, ML risk model and C4 are roadmap.
9. Ask the project owner first before: deleting files, force-push, adding a remote or pushing,
   installing system software, changing pinned versions, any hosted-LLM run beyond a 5-call smoke test.
10. Never weaken a test to make it pass.

Also: no LLM-written SQL anywhere (SQLAlchemy, parameterised queries only); build LLM clients and
embeddings lazily, never at import time; the default `pytest` run needs no Docker, network or keys.

## Commands (Windows PowerShell)

```powershell
.\tasks.ps1 setup             # create .venv (Python 3.12 via uv) and install requirements + dev tools
.\tasks.ps1 test              # offline test tier (no Docker, network or keys)
.\tasks.ps1 lint              # ruff check + mypy engine/
.\tasks.ps1 db-up             # SQL Server 2022 in Docker
.\tasks.ps1 db-init           # apply db/*.sql in order (re-runnable)
.\tasks.ps1 test-integration  # pytest -m integration (needs Docker SQL Server)
.\tasks.ps1 sim               # simulate a day, ingest it, run data-quality checks (needs DB)
.\tasks.ps1 demo              # Phase 3 demo: planner -> gate -> executor on a simulated day
.\tasks.ps1 mocks             # all mock integration services on http://127.0.0.1:8100
.\tasks.ps1 console           # Streamlit console
.\tasks.ps1 chat              # CLI chat with the explainer assistant
```

Without the task runner (any OS): `python -m pytest`, `ruff check .`, `mypy engine`.

## Layout

`engine/` engine code; `api/` FastAPI service; `console/` Streamlit UI; `mcp_servers/` MCP
servers; `db/` SQL scripts; `policy/` YAML policy; `sim/` simulator; `eval/` replay and results;
`tests/` all tests.
