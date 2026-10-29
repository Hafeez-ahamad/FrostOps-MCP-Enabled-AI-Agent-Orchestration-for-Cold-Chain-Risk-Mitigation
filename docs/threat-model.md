# Threat model

**Scope:** the dispatch engine (detection, planner, LLM roles, gate, executor), its database, the API and console, and the MCP servers. Everything runs on a simulated fleet, but the controls are built as if it were real.

**Assets:**
- the integrity of actions on vehicles and cargo;
- the approval record;
- the audit trail;
- telemetry and depot data, which are confidential per depot;
- credentials.

## Threats, controls and the tests that prove them

| # | Threat | Example | Controls | Tests |
|---|---|---|---|---|
| T1 | **Prompt injection** via data | Hostile text in an SOP chunk, a facility name or a text telemetry field tells the agent to pick another action | Data reaches prompts only inside tags, with `<`/`>` escaped (`engine/agents/context.py`); the model can only pick an index from the deterministic candidate list; the gate re-checks everything; L3 needs a human | `tests/unit/test_injection.py` (hostile strings × locations), `test_data_cannot_close_its_tag`; H3 replay in `eval/EVAL.md` |
| T2 | **Tool abuse** by an agent | The planner LLM tries to execute, or the explainer tries to write | MCP servers register only the role's allow-listed tools (`policy/mcp_roles.yaml`), checked server side; `submit_action` only submits to the gate; separate DB logins per role (`db/roles.sql`) | `tests/unit/test_mcp_servers.py`; `tests/integration/test_roles_sqlserver.py` |
| T3 | **Privilege escalation between agents** | The planner's DB identity inserts into `ops.Actions`; the executor reads raw telemetry | Per-role logins with minimal grants plus DENY on the raw table and `dbo` writes; the executor login has no SELECT on telemetry | `test_planner_cannot_insert_actions`, `test_executor_cannot_read_raw_telemetry`, `test_explainer_cannot_write` (integration) |
| T4 | **Replayed or forged approval** | Reusing yesterday's approval, approving a modified proposal, approving as the wrong role | The approval is bound to the proposal's sha256; one decision per proposal (unique index); 30-min TTL; role must be in `approver_roles`; the executor re-checks the stored approval and hash before running | `test_invariant_2_*` (hash, reuse, TTL, role, L4) |
| T5 | **Gate bypass** | An action row inserted directly; a forged decision object | The executor authorises from the database (approved proposal, same hash, stored human approval for L3) before enqueueing and before every attempt | `test_invariant_1_*` |
| T6 | **Kill-switch bypass** | Actions keep running after a supervisor stops them | The switch is checked at the start of every poll and before every action; the DB flag can only switch it on over the YAML default; changes are audited | `test_invariant_4_*`, `test_control_changes_are_audited` |
| T7 | **Poisoned or glitchy telemetry** | A spike or °F reading triggers a diversion | Data-quality layer (valid/suspect/invalid); L2 needs `data_quality_ok`, verified against stored rule hits; stale telemetry blocks L2 | `test_invariant_5_*`, `eval/results/phase1_dq_detection.csv` |
| T8 | **Audit tampering or loss** | Editing or deleting audit rows; acting while the audit write fails | Append-only, hash-chained `ops.AuditLog` (`verify_chain`); insert-only grants; audit is written before each step, and if that fails the step does not run (fail closed) | `test_audit_chain_verifies_and_detects_tampering`, `test_executor_does_not_call_the_integration_when_audit_fails` |
| T9 | **Data exfiltration across depots** | A dispatcher reads another depot's shipments | Row-level security by depot (`SESSION_CONTEXT`, read-only, never pooled); the API filters by the user's depots | `test_console_sees_only_its_depots` (integration), `test_dispatcher_sees_only_their_depots` (API) |
| T10 | **Credential leakage** | Passwords in the repo or logs | Secrets only from `.env` or Docker secrets; gitleaks in CI; API tokens stored only as sha256; `SecretStr` in settings | CI gitleaks job; `test_scripts_target_coldchaindb_and_hold_no_secrets` |
| T11 | **LLM outage or garbage** | The model is down, slow, or returns invalid JSON | Every failure becomes "no answer"; the deterministic fallback runs, marked degraded; token budget per run | `test_malformed_llm_output_falls_back_to_c0_and_is_marked_degraded`, `test_unavailable_llm_degrades_to_c0` |
| T12 | **Unsafe policy change** | Someone lowers an L3 action to L2 through the API | `PUT /admin/policy`: supervisor/admin only; validated; refuses lowering any action's level or removing data-quality checks from L2; versioned and audited | `test_policy_update_cannot_lower_autonomy_level` (API) |
| T13 | **Database outage or schema drift** | Writes time out; the source renames a legacy column | 3 tries, then alert-only mode: detect and alert, but no dispatch and no executor; a drifted reading is refused; only a person clears the mode (audited) | `tests/unit/test_failure_injection.py` (one test per spec §14 row) |

## Residual risks (accepted for a simulated build)

- **Not yet run:** the SQL Server-side controls (grants, RLS, unique indexes) are tested only in the integration tier, which has not run on this machine (no Docker).
- **Model scope:** a local 7B model was the only model evaluated; a different model may follow injected text differently.
- **Demo tokens:** the demo API uses bearer tokens generated at start-up, not SSO, and serves plain HTTP on localhost.
- **No denial-spike alert:** gate rejections are counted (`coldchain_gate_decisions_total`) but there is no automatic alert on a spike yet.
- **Mock services:** they cannot reproduce every real integration failure mode.
