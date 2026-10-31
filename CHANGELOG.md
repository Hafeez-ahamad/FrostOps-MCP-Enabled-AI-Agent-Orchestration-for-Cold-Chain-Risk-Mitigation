# Changelog

## 0.1.0 — 2026-10-30

First version: policy-bounded autonomy on a **simulated** fleet.

- **Simulator and data layer.** Seeded fleet and scenarios with six incident types and eight noise
  types, append-and-dedup ingestion, and a data-quality layer (`valid`, `suspect`, `invalid`).
- **Rules and incidents.** SOP-linked deterministic rules with boundary tests, a documented risk
  score, and incident open, merge and clear.
- **Planner, gate, executor.** Deterministic candidates and costing, a default-deny policy gate with
  approvals bound to the proposal hash, an executor with retries, compensation and escalation, mock
  services with injectable faults, and a hash-chained audit log that fails closed.
- **LLM roles.** Monitor, planner and explainer agents with Pydantic-validated output, three
  configurations (C0 deterministic, C1 single agent, C2 multi-agent), and fallback to C0 when the
  model is unavailable or malformed.
- **Evaluation harness.** Replay with paired bootstrap confidence intervals, a counterfactual
  exposure model, a red-team configuration without the gate, and an injection variant. Campaign
  results are not yet measured.
- **API, console and MCP.** FastAPI service with bearer tokens, roles and depot scope, kill switch,
  versioned policy updates, a Streamlit console, and four MCP servers with per-role allow-lists.
- **Security and operations.** Per-agent database logins, a threat model, prompt-injection tests,
  alert-only mode, JSON logs, Prometheus metrics, and a runbook.
