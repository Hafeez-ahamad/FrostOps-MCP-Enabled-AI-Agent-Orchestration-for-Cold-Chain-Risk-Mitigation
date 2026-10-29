# LLM roles and the C0 / C1 / C2 configurations (Phase 4)

All three configurations share every deterministic part:
- detection (data quality, SOP rules, incidents);
- the planner's candidate actions and their costs;
- the policy gate and the executor;
- the mock services, the seeds, and one token budget per run.

They differ only in where an LLM may weigh in. **No LLM decides a threshold, a permission, or whether an action runs**: every proposal goes through the same gate.

| Config | Monitor (triage) | Choice among candidates | Explainer |
|---|---|---|---|
| **C0** deterministic | none | top-ranked candidate | template, or LLM if `explain` is on |
| **C1** single agent | inside the single call | one LLM call sees everything (incident, telemetry, candidates, facilities, SOP) and returns verdict + choice; asked whenever there are 2+ candidates | same |
| **C2** multi-agent | LLM, temperature incidents only; sees incident, cargo band and recent temperatures | deterministic, unless the plan is a close call; then the planner LLM sees candidates, facilities and SOP, but no telemetry | same |

C3 (C2 without the gate) is red-team only and is built in Phase 5 under `eval/redteam/`, never here.

## How an LLM answer can change a decision

- **Triage** (C2 monitor, or the C1 verdict). A confident (≥ 0.7) `door_open_event` verdict re-weights the candidates:
  - recording the excursion (`create_incident_ticket`) becomes the best candidate;
  - notifying the driver drops below it;
  - the fallback escalation drops further.

  Notifying the driver stays a candidate. A triage verdict can never remove every action.
- **Choice** (C1 single agent, or the C2 planner). It is an index into the deterministic candidate list. The model cannot invent an action or a target, and an index outside the list counts as no answer.
- **The gate still decides.** An LLM choice of an L3 action still waits for a named human (`test_an_llm_choice_still_needs_a_human_for_l3`).

## Fallbacks and "degraded"

`engine/llm/structured.py` turns every LLM problem into "no answer":
- model unavailable;
- token budget exhausted;
- reply not a JSON object, or failing the Pydantic schema;
- choice out of range;
- explainer citing a rule or clause it was not given.

The graph then uses the deterministic answer, and the step is recorded as degraded:
- in `Runtime.degraded_steps`;
- in the proposal (`degraded: true`);
- in the `PlanProposed` message.

Tests show that a garbage-speaking or unavailable LLM in C1/C2 produces exactly C0's actions (`test_malformed_llm_output_falls_back_to_c0_and_is_marked_degraded`, `test_unavailable_llm_degrades_to_c0`).

## Prompts and injection

- **Delimited data.** Telemetry, incident evidence, candidates, facility names and SOP text reach the model only inside tags (`<telemetry>`, `<candidates>`, `<sop_clause>`, ...). Each prompt says the content is data, not instructions.
- **Escaped tags.** `engine/agents/context.data_block` escapes `<`, `>` and `&`, so hostile text cannot close a tag and pose as instructions (`test_data_cannot_close_its_tag`).
- **Least privilege between roles** (C2):
  - the monitor never sees candidates or facilities;
  - the planner never sees raw telemetry (`test_monitor_sees_only_incident_band_and_telemetry`, `test_planner_agent_sees_no_raw_telemetry`).
- **The injection test set** is `tests/unit/test_injection.py`: hostile strings in an SOP chunk, a facility name, incident evidence and the `HZD_TIER` field, plus an "obedient" scripted LLM that follows the planted text. It still causes 0 unauthorised actions, and L3 still waits for a human. The replay's injection variant (H3) measures how often a real model follows the planted text.

## Messages, logging, durability

- **Typed messages.** `IncidentOpened`, `PlanProposed`, `ActionApproved`, `ActionExecuted` and `ActionFailed` are Pydantic models in `ops.EventQueue`, each with a correlation id `COR-<incident>`.
  - The monitor publishes the incident, and the plan node consumes it.
  - The planner publishes proposals, and the gate consumes them.
  - Executor outcomes and human approvals are published too.
- **LLM call log.** Every call is a row in `ops.LLMCalls`, including refused and failed ones. Each row has the role, model and version, prompt and completion tokens (flagged if estimated), latency, error, and the prompt and completion text.
- **Durable chat memory.** The chat assistant's memory is a LangGraph SQLite checkpointer in `data/state/`, which is gitignored (`test_chat_memory_survives_a_restart`). The decision graphs are short-lived per incident event; their durable record is the database (proposals, decisions, audit, queue).

## What has and has not been run

- **Tests:** a scripted stand-in LLM (`engine/llm/fake.py`) drives the offline tests. It shows that the plumbing works end to end for C0, C1 and C2 on the smoke scenario. **It is not evidence of LLM quality.**
- **Real LLM:** the Phase 5 replay runs local Ollama `qwen2.5:7b` (temperature 0, seed 0). The model digest and Ollama version are in `eval/results/phase5/<campaign>/manifest.json`. No hosted model has been called, because that needs the project owner's approval (`docs/engineering-rules.md` rule 9).
  - `python -m eval.llm_smoke` makes at most 5 calls, one per role, and prints whether each answer validated. It has not been run on its own.
- **Comparison:** the C0 / C1 / C2 results are in `eval/EVAL.md` Part 2. They are filled in only from the generated CSVs.
- **LLM metrics:** every call also increments `coldchain_llm_calls_total{role,result}` and `coldchain_llm_tokens_total{role}`, and logs a JSON line with its correlation id.
