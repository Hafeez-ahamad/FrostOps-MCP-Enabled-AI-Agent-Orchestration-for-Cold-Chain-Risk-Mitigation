# Autonomy policy (as built, Phases 3 and 6–7)

This engine has **policy-bounded autonomy on a simulated fleet**. No LLM decides whether an action runs. The policy gate (`engine/gate/gate.py`) and the executor (`engine/executor/executor.py`) are deterministic code. The catalogue in `policy/actions.yaml` decides what each action is allowed to do.

## Levels and the action catalogue

| Action | Level | Reversible | Preconditions | Limits |
|---|---|---|---|---|
| `notify_driver_restart_cooling` | L2 auto | yes | rule breach confirmed, data quality ok, driver reachable | 10 min cooldown; 3 per shipment per hour |
| `create_incident_ticket` | L2 auto | yes | rule breach confirmed, data quality ok | 60 min cooldown; 1 per shipment per hour |
| `divert_to_cold_storage` | L3 approve | until departure | rule breach confirmed, facility capacity ok, ETA delay > 60 min | approvers: dispatcher, supervisor |
| `divert_to_overflow_depot` | L3 approve | until departure | rule breach confirmed, facility capacity ok | approvers: dispatcher, supervisor |
| `escalate_tier2_manager` | L3 approve | no | none (listed explicitly as `[]`) | approver: dispatcher |
| `reject_load` | L4 never | no | — | never executes |

**Global limits:**
- at most 30 automatic (L2) actions per hour;
- an approval is valid for 30 minutes;
- the kill switch and dry run are off by default.

A supervisor can switch on the kill switch or dry run in `ops.ControlFlags`. The database can only make the policy safer: either source saying "on" means on.

The YAML file's sha256 is stored with every gate decision. That records which policy version made each decision.

## What the gate checks (in order)

1. **Duplicate.** The proposal id is derived from (incident, action, target). Resubmitting the same triple is a `duplicate` and changes nothing.
2. **Known action.** An action not in the catalogue is rejected (default deny).
3. **Level.** L0, L1 and L4 are rejected (not executable).
4. **Limits:**
   - kill switch;
   - cooldown since this action last ran on this shipment;
   - per-shipment hourly limit;
   - global hourly limit for L2.
5. **Preconditions.** Every listed precondition must pass, and every L2 action also needs `data_quality_ok`, even if the YAML forgot it.
   - **Evidence is checked against the database.** The gate does not take the proposal's word: it looks up the cited rule hits in `ops.RuleHits`, where detection stored each one with its reading's data-quality status. A hit on a suspect or invalid reading, or one never recorded, fails.
   - **Stale telemetry fails `data_quality_ok`.** A freshness flag after the evidence means the data is stale.
   - **Service checks fail closed.** `driver_reachable` and `facility_capacity_ok` call the services; if a service cannot answer, the precondition is not met.
6. **Routing:** L2 → execute, or `dry_run` when dry run is on. L3 → wait for a human.
7. **Audit first.** The decision is written to the audit log before it takes effect. If that write fails, the answer is "rejected (audit unavailable)" and nothing is stored or run.

## Drafts (MCP planning server)

An agent working through the MCP servers cannot submit straight to the executor. It works in two steps:
1. `create_proposal` (planning server) stores a **draft**.
   - The evidence comes from `ops.RuleHits` for the incident's latest reading, not from the caller.
   - On SQL Server the planner login can only call `ops.usp_InsertDraftProposal`, which always writes `Status = 'draft'`.
2. `submit_action` (action server, a different role) runs `submit_draft`.
   - This re-hashes the stored payload. A draft edited after it was written is rejected (`proposal_hash_mismatch`).
   - Otherwise it applies exactly the checks above.
   - A second submission is a `duplicate`.

## Policy versions

- **Changes.** The catalogue changes at runtime only through `PUT /admin/policy` (`engine/gate/policy_admin.py`), by a supervisor or admin.
- **Refused:**
  - any change that makes an action more autonomous (L3 → L2, L4 → executable);
  - an L2 action losing a precondition;
  - a new L2 action without `data_quality_ok`;
  - YAML that does not validate.
- **Allowed:** stricter changes, and removing an action, which the gate then denies by default.
- **Recorded.** Every accepted version is audited (`policy_updated`) and stored in `ops.PolicyVersions` with its sha256, author and reason. The gate, executor and planner switch to it together.

## Human approval of an L3 proposal

An approval names a person and a role, and quotes the proposal's sha256. The gate rejects it if:
- the proposal is not awaiting approval (it was already decided);
- the approval id was used before, or the proposal already has a decision;
- the hash differs from the stored proposal (it was edited after review);
- the role is not in the action's `approver_roles`;
- the approval is older than the 30-minute TTL, older than the proposal, or dated in the future.

If the approval passes those checks, the gate checks the kill switch, limits and preconditions again, because the facility may have filled up while the proposal waited. Only then does it hand the action to the executor.

## The executor

**States:** `queued → executing → succeeded | failed | cancelled`.

**Authorisation:** before taking or running anything, the executor checks the database itself:
- the proposal must be stored as approved, with the same hash;
- for L3, a human approval of that exact hash must be stored.

So an action row inserted directly, or a forged decision object, never runs.

**Idempotency:** one action per `incident:action:target`, enforced by a unique index. The same key is sent to the service, so a retry never repeats a side effect.

**Fail closed:** the `action_executing` audit entry is written before each call. If that write fails, the call is not made and the action stays queued.

**Retries:**
- Transport errors, 5xx responses, timeouts, partial responses and an unreachable driver are retried with exponential backoff (60 s, 120 s) in simulated time.
- After 3 attempts the action fails.
- When an action fails for good, the dispatcher marks the incident `needs_attention` and proposes a Tier 2 escalation, which still needs human approval.

**Kill switch:** checked at the start of every poll and before every action, so it takes effect within one polling interval.

**Cancel:**
- A queued action is simply cancelled.
- A succeeded reversible action is compensated: close the ticket, cancel the diversion (which frees the slot), or tell the driver to disregard the instruction.
- An irreversible action (the escalation) cannot be cancelled.

## Audit log

`ops.AuditLog` is append-only and hash-chained: each row stores the sha256 of the previous row's hash plus its own content. `engine.audit.log.verify_chain` detects an edited or deleted row. It records every decision, approval, control change and action step, together with who made it: `policy_gate`, `executor`, or `role:person` for humans.

## Safety invariants (spec §4) and their tests

| Invariant | Tests (`tests/unit/test_safety_invariants.py`) |
|---|---|
| 1. No action runs unless the gate approves it | `test_invariant_1_no_action_runs_without_gate_approval`, `…_executor_rejects_a_decision_the_gate_did_not_make`, `…_unknown_actions_are_denied` |
| 2. L3/L4 never execute without a recorded human approval | `test_invariant_2_l3_and_l4_never_execute_without_recorded_human_approval` + hash binding, reuse, TTL, role, L4-with-approval tests |
| 3. Same incident never triggers the same action twice | `test_invariant_3_same_incident_never_triggers_the_same_action_twice`, `…_cooldown_blocks_a_repeat_for_a_new_incident` |
| 4. Kill switch halts within one polling interval | `test_invariant_4_kill_switch_halts_execution_within_one_polling_interval`, `…_kill_switch_stops_mid_poll` |
| 5. Data-quality failures block action | `test_invariant_5_data_quality_failure_blocks_action`, plus checks for evidence verified against the database, the YAML forgetting data quality, and stale telemetry |

**Mutation check.** These tests were checked against deliberately broken safeguards:
- executor authorisation removed;
- approval hash check removed;
- kill switch ignored;
- evidence trusted without the database lookup;
- duplicate detection removed.

Each break made at least one invariant test fail. The breaks were applied as temporary test patches, not committed.

## Known limits

- **Logins not yet tested on a server.** Each role has its own login and grants (`db/roles.sql`, `RoleEngines`), but the SQL Server tests (`tests/integration/test_roles_sqlserver.py`) have not run: there is no Docker on the development machine. The replay and demo use one in-memory database for all roles.
- **Replay approver.** Replay uses a scripted stand-in approver. Real approvals come from authenticated users through the API (`POST /proposals/{id}/approve`).
- **Planner costs are illustrative.** The weights were chosen so the ranking follows the SOP, and are documented in `engine/planner/planner.py`.
- **The C0 planner cannot tell a door-open event from a reefer failure.** Both look the same to the rules, so it notifies the driver in both cases; for a door-open breach the oracle prefers just a ticket. Hypothesis H2 (`eval/EVAL.md`) tests whether LLM triage helps.
- **High Risk can arrive before the breach.** When a long delay makes a truck "High Risk" before its temperature breaches, the engine first proposes the SOP §3 escalation and later the SOP §1 diversion. Both follow the SOP; the replay scores this against the oracle.
