# Progress

Tick an item only when the proof in the right-hand column exists and passes.
Legend: `[x]` done with proof, `[ ]` not done, `[~]` partly done (see note).

## Definition of done (minimum viable cut, spec §17)

| # | Criterion | Proof | Status |
|---|---|---|---|
| 1 | Rules engine is deterministic with boundary tests; no LLM decides a threshold | `tests/unit/test_rules.py` (boundaries, Hypothesis properties, SOP traceability, `test_deterministic_core_imports_no_llm_library`) | [x] |
| 2 | Policy gate default-denies; zero unauthorised actions across all replay runs of C0–C2 | `test_invariant_1_unknown_actions_are_denied`; `eval/results/phase3_demo_summary.csv` (C0, smoke: 0 unauthorised); C1/C2 replay in Phase 5 | [~] C0 demo only |
| 3 | Each agent has its own DB identity; planner cannot execute, executor cannot read raw telemetry | `db/roles.sql`, `RoleEngines` (`engine/runtime.py`); `tests/integration/test_roles_sqlserver.py` (`test_planner_cannot_insert_actions`, `test_executor_cannot_read_raw_telemetry`, `test_explainer_cannot_write`, audit no-update/delete, draft procedure) | [~] written; not run (no Docker) |
| 4 | Data-quality layer stops seeded glitches from triggering L2 actions | Phase 1: glitches flagged (`test_dq_on_scenario.py`, `eval/results/phase1_dq_detection.csv`); Phase 3: `test_invariant_5_data_quality_failure_blocks_action` and siblings | [x] |
| 5 | All five safety invariants (spec §4) pass as named tests | `tests/unit/test_safety_invariants.py` (`test_invariant_1_…` to `test_invariant_5_…`), mutation-checked | [x] |
| 6 | Replay results for C0, C1, C2 (and C3 red-team) with CIs in `eval/results/` and `eval/EVAL.md` | `eval/replay.py`, `eval/report.py`; campaigns not yet run on the current code | [ ] not yet measured |
| 7 | Failure-injection table (spec §14) with a test per row; fail-closed audit demonstrated | `tests/unit/test_failure_injection.py` (weather/congestion down or slow, driver API fails, transient and persistent DB failure, audit fails, LLM malformed/unavailable, duplicated events, schema drift) | [x] |
| 8 | `pytest` offline passes; `pytest -m integration` passes against Docker; CI green; gitleaks clean | offline: 380 passed (2026-10-29); integration, CI, gitleaks: not run | [~] offline only |
| 9 | README follows spec §19 | `README.md` (order of §19); replay results and the demo GIF still TBD | [~] |
| 10 | All Phase 7 docs exist and match the code | `docs/runbook.md`, `docs/architecture.md`, `docs/threat-model.md`, `docs/data-card.md`, `docs/scoping-note.md`; Terraform and CI/CD with rollback are roadmap (`docs/engineering-rules.md` rule 8) | [x] MVP docs |
| 11 | `docs/demo-script.md` for the 3-minute video | `docs/demo-script.md` (timeline checked offline); video not recorded | [x] script |

## Phase 0 — Foundation

| Item | Proof | Status |
|---|---|---|
| System prompt loaded in one place for every entry point | `tests/unit/test_prompt_loading.py` | [x] |
| Tool modules import with no keys, no network, no torch | `tests/unit/test_clean_imports.py` | [x] |
| No LLM-written SQL; fixed parameterised telemetry tools | `tests/unit/test_telemetry_tools.py` | [x] |
| Offline SOP retrieval by default | `tests/unit/test_sop_retrieval.py` | [x] |
| Pinecone as an optional SOP backend | deferred (see decisions) | [ ] |
| Console renders offline | `tests/unit/test_console.py` | [x] |
| Docs aligned with code (README, setup) | `README.md`, `.env.example`, `tasks.ps1` | [x] |
| CI: ruff + mypy + pytest + gitleaks | `.github/workflows/ci.yml` | [~] written; not run (no remote yet) |
| Secret scan clean | local case-insensitive grep clean; gitleaks runs in CI | [~] gitleaks not run yet |
| `db/raw_schema.sql`, `db/roles.sql`, `docker-compose.yml`, `db-init` | files exist | [~] not run (Docker not installed) |

## Phase 1 — Simulator and data layer

Spec "done when": reproducible scenario by seed; DQ flags the seeded glitches.

| Item | Proof | Status |
|---|---|---|
| Seeded fleet (50–200 vehicles, SoCal depots, cargo bands from config, facilities with capacity) | `tests/sim/test_generator.py` (`test_fleet_is_in_southern_california_with_configured_cargo`, `test_scenario_rejects_fleet_size_outside_spec`) | [x] |
| Reproducible by seed | `test_same_seed_gives_identical_output`, `test_different_seed_gives_different_output` | [x] |
| Documented physics | `sim/physics.py`, `docs/simulator.md`, `tests/sim/test_physics.py` (Hypothesis) | [x] |
| All 6 incident types with ground truth and oracle action | `test_every_incident_type_is_seeded_and_labelled` and per-type tests | [x] |
| All 8 noise types with labels | `test_every_noise_type_is_seeded_on_incident_free_shipments`, `test_glitches_change_the_stream_as_documented` | [x] |
| Raw telemetry columns (`CNSG_NO`, `TRLR_NO`, `HUB_CD`, `PROD_CLS`, ...) | `engine/data/schema.py`, `db/raw_schema.sql` | [x] |
| Append + dedup ingestion (not `replace`) | `tests/unit/test_data_layer.py` (SQLite); `test_ingest_twice_inserts_nothing` (SQL Server) | [~] SQL Server test not run |
| DQ layer → `valid / suspect / invalid` in `DataQualityFlags` | `tests/unit/test_dq_checks.py` (boundaries), `test_runner_matches_the_in_memory_stream` | [x] |
| DQ flags the seeded glitches; clean and incident readings untouched | `tests/unit/test_dq_on_scenario.py`; `eval/results/phase1_dq_detection.csv` (seeds 1–3: recall 1.0 on every type; 1 false flag in 71,828 clean readings; 0 incident readings hidden) — **simulated** | [x] |
| `VW_LATEST_VALID_READING` (one row per shipment) | `db/ops_schema.sql`; Python reference `engine/data/latest.py`; `test_latest_valid_view_matches_the_python_reference` | [~] SQL Server test not run |
| Row-level security by depot | `db/roles.sql`; `test_console_sees_only_its_depots`, `test_console_without_a_depot_scope_sees_nothing`, `test_depot_scope_cannot_be_changed_by_the_session` | [~] not run |
| View-plus-DENY and append-only ingest login | `test_readers_cannot_touch_the_raw_table`, `test_ingest_login_cannot_delete_or_update` | [~] not run |
| Schema drift check (spec §14 row) | `test_schema_drift` | [~] not run |
| Kaggle CSV as optional stress set, separate command | `engine/data/kaggle.py`, `tasks.ps1 load-kaggle`, `test_kaggle_rows_map_to_legacy_columns` | [x] mapping tested; load not run |
| CI integration job (SQL Server 2022) | `.github/workflows/ci.yml` `integration` | [~] written; not run (no remote) |

Integration tests (`tests/integration/`) have never run: Docker and ODBC Driver 18 are not
installed on the development machine, and there is no remote for CI yet.

## Phase 2 — Rules, risk, incidents

Spec "done when": boundary tests green; detection recall measured on seeded data.

| Item | Proof | Status |
|---|---|---|
| `policy/sop_rules.yaml` with R-TEMP-01, R-CONG-01, R-RISK-01, R-ETA-01, each linked to its SOP clause | `test_every_rule_quotes_its_sop_clause_verbatim`, `test_thresholds_in_the_yaml_match_the_sop` | [x] |
| Boundary tests 4.0/4.01, 7.0/7.01, 0.65/0.66 (and 60/60.1 min) | `test_r_temp_01_fresh_boundary`, `test_r_cong_01_boundary`, `test_r_risk_01_boundary`, `test_r_eta_01_boundary` | [x] |
| Hypothesis property tests on every boundary | `test_temperature_rule_is_exactly_strict_greater_than_band_max` and the three siblings | [x] |
| No LLM in the deterministic core | `test_deterministic_core_imports_no_llm_library` | [x] |
| Invalid readings never evaluated; suspect hits kept for triage, never open incidents | `test_invalid_readings_are_never_evaluated`, `test_non_valid_readings_never_open_or_clear` | [x] |
| Documented risk formula | `engine/risk/score.py`, `docs/rules-and-incidents.md`, `tests/unit/test_risk.py` | [x] |
| Incident open / merge / clear; one open incident per shipment | `tests/unit/test_incidents.py`; DB index test `test_database_allows_only_one_open_incident_per_shipment` | [~] DB test not run |
| `ops.RuleHits`, `ops.Incidents` + store | `db/ops_schema.sql`, `engine/incidents/store.py`, `test_store_appends_hits_and_upserts_incidents`, `test_schema_drift` | [~] SQL Server tests not run |
| Detection recall and false-alarm rate measured on seeded data | `eval/results/phase2_detection.csv`, `eval/results/phase2_false_alarms.csv` (detection_eval, seeds 1–3: 123 of 123 expected incidents detected; 0 false alarms in 2,678 shipments; 160 of 160 glitched readings that would have crossed a threshold stopped by data quality) — **simulated** | [x] |


## Phase 3 — Planner, gate, executor, mocks

Spec "done when": safety invariants (§4) pass as tests; two auto actions and one approval-gated action demo.

| Item | Proof | Status |
|---|---|---|
| Deterministic candidate generation and costing | `engine/planner/planner.py`, `tests/unit/test_planner.py` | [x] |
| `policy/actions.yaml` as spec §4 (2+ L2, 1+ L3, L4; preconditions, cooldowns, rate limits, kill_switch, dry_run) | `test_catalog_matches_the_spec_minimum`, `test_preconditions_must_be_listed_explicitly` | [x] |
| Five safety invariants as named tests | `tests/unit/test_safety_invariants.py` (19 tests), mutation-checked | [x] |
| Approvals bound to the proposal hash; reused/old approvals rejected | `test_invariant_2_approval_is_bound_to_the_exact_proposal_hash`, `…_a_reused_approval_is_rejected`, `…_an_old_approval_is_rejected` | [x] |
| Idempotency key per (incident, action, target) | `test_invariant_3_same_incident_never_triggers_the_same_action_twice`; unique index `UQ_Actions_IdempotencyKey` | [x] |
| Executor states, retries with backoff, escalation on failure | `tests/unit/test_executor.py`, `test_failed_action_marks_needs_attention_and_proposes_escalation` | [x] |
| Cancel path and compensation | `test_cancel_a_queued_action`, `test_cancel_a_succeeded_reversible_action_compensates`, `test_irreversible_succeeded_action_cannot_be_cancelled` | [x] |
| Mock services (driver, ticketing, TMS, facilities, weather, congestion): FastAPI apps with injectable failure, latency, partial responses | `sim/mock_services/`, `tests/unit/test_mock_services.py` | [x] |
| Append-only, hash-chained AuditLog written before each action; fail closed | `tests/unit/test_audit_and_policy.py` (`test_executor_does_not_call_the_integration_when_audit_fails`, `test_gate_rejects_when_the_audit_write_fails`, chain tamper tests) | [x] |
| Demo: two auto actions and one approval-gated action | `eval/phase3_demo.py` -> `eval/results/phase3_demo_*.csv` (smoke, simulated: 9 L2 + 15 L3 succeeded, 0 unauthorised, audit chain valid); `test_two_auto_actions_and_an_approval_gated_action_run` | [x] |
| New ops tables on SQL Server | `db/ops_schema.sql`; `test_schema_drift`, `test_l2_and_approved_l3_run_on_sql_server_with_a_valid_audit_chain` | [~] not run |


## Phase 4 — LLM roles and configurations

Spec "done when": all three configurations run end to end on one scenario.

| Item | Proof | Status |
|---|---|---|
| One LLM interface: Ollama default, one hosted backend, temperature 0, built lazily | `engine/llm/factory.py`, `test_factory_builds_the_llm_lazily`, `test_engine_imports_without_keys_or_heavy_modules` | [x] |
| Model, version, prompt/completion tokens and latency logged for every call | `ops.LLMCalls`; `test_every_call_is_logged_with_tokens_latency_and_text` | [x] |
| Monitor, Planner, Explainer agents and a single-agent baseline, Pydantic-validated output | `engine/agents/`, `tests/unit/test_agents.py` | [x] |
| `c0_deterministic.py`, `c1_single.py`, `c2_multi.py` sharing tools, gate, executor, seeds, token budget | `engine/graphs/`, `test_all_three_configurations_run_end_to_end` | [x] (scripted LLM) |
| Typed messages through a persisted queue with correlation ids | `engine/bus/`, `ops.EventQueue`, `test_every_handoff_is_a_persisted_typed_message`, `tests/unit/test_llm_and_bus.py` | [x] |
| Durable checkpointer instead of MemorySaver | `engine/chat/service.py` (SQLite saver), `test_chat_memory_survives_a_restart` | [x] |
| Malformed/missing LLM output falls back to deterministic; run marked degraded | `test_malformed_llm_output_falls_back_to_c0_and_is_marked_degraded`, `test_unavailable_llm_degrades_to_c0`, `test_exhausted_budget_degrades_and_logs_the_refusals` | [x] |
| Prompts treat telemetry, SOP and tool output as delimited data | `engine/agents/context.py`, `test_data_cannot_close_its_tag`, least-privilege prompt tests | [x] |
| Run with a real LLM (Ollama or hosted) | Phase 5 replay on local Ollama `qwen2.5:7b` (`eval/results/phase5/mvp/manifest.json`, once run); hosted needs approval | [ ] not yet run |


## Phase 5 — Replay evaluation

Spec "done when": results table published for C0–C3.

| Item | Proof | Status |
|---|---|---|
| Hypotheses H1–H4, metrics, slices and protocol pre-registered before any run | `eval/EVAL.md` Part 1, commit `708b1c1` | [x] |
| Replay harness: configurations × seeds × blocks × variant, one atomic result file per run, resumable | `eval/replay.py`, `test_replay_writes_a_complete_result` | [x] |
| Metrics, paired bootstrap CIs, slices | `eval/metrics.py`, `eval/stats.py`, `tests/unit/test_eval.py` | [x] |
| Counterfactual exposure model (simulated, open-loop simulator) | `sim/counterfactual.py`, `test_restoring_cooling_earlier_never_increases_exposure` and siblings | [x] |
| C3 red team (C2 without the gate), never imported by `engine/` | `eval/redteam/c3_no_gate.py`, `test_c3_executes_what_the_gate_would_have_held`, `test_engine_never_imports_eval_or_the_red_team` | [x] |
| Injection variant (hostile facility name and SOP line) | `eval/injection.py`, `test_hostile_text_lands_where_intended` | [x] |
| MVP campaign (seeds 1–3) and final campaign (seeds 1–5 × 3 blocks, injection seeds 1–5) with local `qwen2.5:7b` | `eval/results/phase5/{mvp,final}/` | [ ] not yet run |
| `EVAL.md` Part 2 and README table, only from the generated CSVs | `python -m eval.report --campaign mvp|final` | [ ] after the campaigns |

## Phase 6 — Console and MCP

Spec "done when": approve/reject flow works; MCP auth tests pass.

| Item | Proof | Status |
|---|---|---|
| FastAPI service: bearer tokens (sha256 only), roles checked per route, dispatchers limited to their depots | `api/`, `tests/unit/test_api.py` (`test_requests_without_a_valid_token_are_401`, `test_each_route_is_forbidden_to_roles_without_the_permission`, `test_dispatcher_sees_only_their_depots`) | [x] |
| Approve/reject bound to the proposal hash; wrong hash rejected; approval cannot be reused | `test_approval_with_a_wrong_hash_is_rejected_and_the_right_hash_executes` | [x] |
| Kill switch, dry run, alert-only clear, audit export with chain check, `/health`, `/metrics` | `test_kill_switch_stops_the_executor_within_one_poll`, `test_audit_export_has_a_valid_chain`, `test_metrics_exposes_the_engine_counters` | [x] |
| Safety-checked, versioned policy updates | `engine/gate/policy_admin.py`; `test_policy_update_cannot_lower_autonomy_level`, `test_policy_update_cannot_drop_an_l2_precondition`, `test_stricter_policy_update_is_accepted_versioned_and_applied` | [x] |
| Console (spec §12 screens) as an API client | `console/app.py`, `tests/unit/test_console.py` (fake API through AppTest: board, approve sends the hash, kill switch) | [x] |
| Four MCP servers with server-side allow-lists per role | `mcp_servers/servers.py`, `policy/mcp_roles.yaml`, `tests/unit/test_mcp_servers.py` (listed tools, "Unknown tool", evidence from the DB, drafts through the gate, edited draft rejected) | [x] |
| MCP tool-call benchmark (spec §11: two model backends) | — | [ ] out of scope: only local Ollama is approved, so at most one backend |
| Approve/reject flow run in the browser | `docs/demo-script.md`. Run on 2026-10-31 on the simulated smoke day (C0, 10 h): wrong hash rejected, right hash executed, reuse rejected over the API; an L3 divert approved in the Streamlit console (built-in browser, driven by the agent, not by the owner) showed `Gate: execute`, then `succeeded` after the clock advanced, audit chain valid | [x] |

## Phase 7 — Security and ops (MVP part)

| Item | Proof | Status |
|---|---|---|
| Per-agent DB logins and grants; planner drafts only through a procedure | `db/roles.sql`, `db/ops_schema.sql`, `engine/db.py`, `RoleEngines` | [~] written; SQL Server tests not run |
| Threat model mapped to controls and tests | `docs/threat-model.md` (T1–T13) | [x] |
| Prompt-injection tests (strings × locations, obedient LLM) | `tests/unit/test_injection.py` | [x] |
| Fail-closed audit with an alert | `test_audit_failure_blocks_the_gate_and_the_executor_and_raises_an_alert` | [x] |
| Alert-only mode on persistent DB failure or schema drift | `test_a_persistent_db_failure_degrades_to_alert_only`, `test_schema_drift_puts_the_engine_in_alert_only_with_a_clear_reason`, `test_leaving_alert_only_needs_a_person_and_is_audited` | [x] |
| JSON logs with correlation ids, Prometheus metrics, alerts | `engine/observability.py`, `tests/unit/test_observability.py` | [x] |
| Stale weather lowers confidence; no L2 depends on it | `test_weather_and_congestion_down_lower_confidence_but_change_no_l2_action`, `test_a_failed_weather_read_falls_back_to_the_cached_value_marked_stale` | [x] |
| Runbook, architecture, demo script, scoping note, data card | `docs/` | [x] |
| Rollback demo (policy version) | `docs/runbook.md` §5; `test_stricter_policy_update_is_accepted_versioned_and_applied` | [~] tested; not demoed |
| Runbook followed by someone else | — | [ ] |
| CI run, gitleaks | `.github/workflows/ci.yml` | [ ] not run (no remote; pushing needs the owner's OK) |
| Terraform, Grafana dashboards, denial-spike alert | — | roadmap |
