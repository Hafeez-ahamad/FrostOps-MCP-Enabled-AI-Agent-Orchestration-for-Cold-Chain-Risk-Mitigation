# Runbook

How to install, configure, start, stop, and recover the dispatch engine. Everything here runs on a **simulated fleet**: mock services stand in for the driver app, ticketing, TMS, facilities, weather and congestion.

## 1. Install

Windows PowerShell, from the repository root:

```powershell
.\tasks.ps1 setup      # .venv with Python 3.12 (uv), requirements and dev tools
.\tasks.ps1 test       # offline tests: no Docker, network or keys
```

Optional:
- **SQL Server.** `.\tasks.ps1 db-up` starts SQL Server 2022 in Docker. `.\tasks.ps1 db-init` then applies `db/*.sql` and is safe to re-run. This needs Docker and ODBC Driver 18; ask the owner before installing either.
- **Local LLM.** C1 and C2 need [Ollama](https://ollama.com) with `qwen2.5:7b` (`ollama pull qwen2.5:7b`). C0 needs no model.

## 2. Configure

Copy `.env.example` to `.env` and replace every placeholder. Never commit `.env`.

| Setting | What it is |
|---|---|
| `MSSQL_SA_PASSWORD` | Administrator. Used only by `db-init`, never by the app. |
| `SQL_AGENT_PASSWORD` | Explainer: read-only (`ccx_reader`). |
| `SQL_INGEST_PASSWORD` | Ingestion and data quality: append-only. |
| `SQL_CONSOLE_PASSWORD` | Console reads, with row-level security by depot. |
| `SQL_MONITOR_PASSWORD`, `SQL_PLANNER_PASSWORD`, `SQL_GATE_PASSWORD`, `SQL_EXECUTOR_PASSWORD` | One login per agent role (`db/roles.sql`, spec §10). |
| `AGENT_LLM`, `OLLAMA_MODEL` | LLM backend. The default is local Ollama. A hosted run needs the owner's approval. |
| `LLM_TOKEN_BUDGET_PER_RUN` | Tokens all LLM roles share in one run. |
| `API_USERS` | API users as `user_id:role:depots:sha256(token)`, comma-separated. Leave it empty for throwaway demo users. |
| `CONSOLE_API_URL`, `CONSOLE_API_TOKEN` | Where the console finds the API, and the token it uses. |

Use the action catalogue as shipped: `policy/actions.yaml`. Change it at runtime only through `PUT /admin/policy` (section 5).

## 3. Start

```powershell
.\tasks.ps1 api -- --config C0 --start-hours 10   # API on http://127.0.0.1:8000
.\tasks.ps1 console                               # Streamlit console (a client of the API)
.\tasks.ps1 mocks                                 # optional: mock services on :8100 for manual fault injection
```

`python -m api` builds an in-memory database, simulates `sim/scenarios/smoke.yaml`, and advances the clock `--start-hours`. If `API_USERS` is not set, it prints four demo tokens once: dispatcher `dana`, supervisor `sam`, auditor `ana` and admin `adi`. Paste one into the console sidebar.

To step the simulated day, use `POST /admin/advance {"minutes": 30}` (supervisor or admin), or the console's *Replay* screen.

## 4. Kill switch and dry run

| What | How | Effect |
|---|---|---|
| Kill switch on | Console *Autonomy panel*, or `POST /admin/killswitch {"on": true}` (supervisor or admin) | The executor runs nothing from its next poll on, and re-checks the switch before every action. The gate rejects new proposals with `kill_switch`. The change is audited (`kill_switch_set`) and raises a `kill_switch` alert. |
| Kill switch off | Same route with `{"on": false}` | Queued actions resume at the next poll. Rejected proposals stay rejected; the next incident event re-plans. |
| Dry run | `POST /admin/dry-run {"on": true}` | The gate answers `dry_run` instead of executing. Nothing is sent to the services. |

The YAML default and the database flag are combined with OR: if either says "on", the switch is on. The database therefore cannot override a YAML that turns the switch on.

## 5. Policy changes and rollback

- **Change:** `PUT /admin/policy {"yaml_text": "...", "reason": "..."}` (supervisor or admin).
- **Accepted** only if no action becomes more autonomous:
  - L3 → L2 and L4 → anything executable are refused;
  - an L2 action losing a precondition is refused;
  - a new L2 action without `data_quality_ok` is refused;
  - making an action stricter, or removing it (default deny), is allowed.
- **Recorded:** each accepted version is stored in `ops.PolicyVersions` (version, sha256, author, reason, YAML) and audited (`policy_updated`). From then on every gate decision carries `policy_version` and `policy_hash`.
- **Roll back:** `GET /admin/policy` shows the current YAML and the history. To go back, `PUT` the earlier version's YAML with a reason. Rolling back to a *looser* version is refused like any other loosening. A loosening needs a code review and a restart with the edited `policy/actions.yaml`, which is deliberate.

## 6. Alert-only mode

The engine switches itself into **alert-only** mode, and `/health` shows `"status": "degraded"` with the reason, when:
- a detection write to the database still fails after 3 tries (timeout, deadlock, connection lost); or
- a reading arrives without the legacy columns (schema drift). That reading is refused.

In alert-only mode:
- incidents are still detected, logged and alerted (`incident_in_alert_only`);
- nothing is planned or dispatched, approvals are not acted on, and the executor does not run.

**To leave it:**
1. Fix the cause: the database, or the source's column mapping. Then check `/health`.
2. Run `POST /admin/alert-only/clear` (supervisor or admin). It is audited (`alert_only_cleared`). If the audit write fails, the mode stays on.

## 7. What to check

| Check | Where | Healthy |
|---|---|---|
| Liveness and mode | `GET /health` | `status: ok`, `alert_only: false`, kill switch as intended |
| Audit chain | `GET /audit` (auditor, supervisor, admin). It reads the whole log to verify the chain, so use it for checks, not for polling | `chain_valid: true` |
| Approval queue | `GET /proposals?status=pending`, or the console's *Approval queue* | Depth is not growing; approvals expire after 30 min |
| Metrics | `GET /metrics` (Prometheus text) | See below |
| Logs | stderr, one JSON object per line | Follow one incident with `correlation_id = COR-<incident id>` |

**Metrics:**

| Metric | What it counts |
|---|---|
| `coldchain_incidents_opened_total` | Incidents opened |
| `coldchain_gate_decisions_total{outcome,action}` | Gate decisions |
| `coldchain_actions_total{status,action}` | Executor outcomes: succeeded, failed, retry_scheduled, refused |
| `coldchain_human_decisions_total{decision}` | Human approvals and rejections |
| `coldchain_llm_calls_total{role,result}`, `coldchain_llm_tokens_total{role}` | LLM calls and tokens |
| `coldchain_approval_queue_depth`, `coldchain_kill_switch`, `coldchain_alert_only_mode` | Gauges |
| `coldchain_alerts_total{kind}` | Alerts raised (section 8) |

## 8. Alerts

An alert is a WARNING log line from `coldchain.alerts` and `coldchain_alerts_total{kind}` + 1.

| `kind` | Meaning | Response |
|---|---|---|
| `audit_unavailable` | An audit write failed. The gate rejected, or the executor held the action without calling the service. | Restore the database. Held actions retry at the next poll. |
| `action_failed` | An action failed after 3 attempts. The incident is `needs_attention` and a Tier 2 escalation waits for approval. | Approve or reject the escalation in the queue. |
| `kill_switch`, `dry_run` | A supervisor switched it on. | Confirm it was intended. |
| `alert_only` | The engine entered alert-only mode (section 6). | Fix the cause, then clear. |
| `incident_in_alert_only` | An incident opened while in alert-only mode. Nothing was dispatched. | Handle it manually. |
| `schema_drift` | A reading was refused because legacy columns are missing. | Fix the source mapping or migration. |

A spike in policy denials has no dedicated alert yet. Watch `coldchain_gate_decisions_total{outcome="rejected"}`.

## 9. Database identities

Each role has its own SQL Server login (`db/roles.sql`):
- the planner can only add drafts, through `ops.usp_InsertDraftProposal`;
- only the gate login changes a proposal's status;
- the executor cannot read telemetry;
- nobody can UPDATE or DELETE `ops.AuditLog`.

The tests for these grants are in `tests/integration/test_roles_sqlserver.py`. They have **not been run yet**: there is no Docker on the development machine.
