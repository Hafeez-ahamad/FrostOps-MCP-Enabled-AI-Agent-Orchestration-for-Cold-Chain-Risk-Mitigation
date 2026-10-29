# Demo script: a 3-minute video

**Story:** the driver-notification service is down. The engine retries, gives up, and escalates. A named dispatcher approves the escalation, and the audit trail shows every step.

Everything on screen is a **simulated fleet** (the smoke scenario, seed 7), using configuration C0, so no LLM is needed. Keep the caption "Simulated fleet · policy-bounded autonomy" visible all the way through.

## Setup (before recording)

```powershell
.\tasks.ps1 api -- --config C0 --start-hours 7 --fault driver.notify=fail
.\tasks.ps1 console
```

1. Copy the printed tokens. You need `sam` (supervisor, moves the clock and controls autonomy) and `dana` (dispatcher, depots DEP-SD and DEP-LB).
2. Open the console in two browser windows, one signed in as **sam** and one as **dana**.
3. In a third window, open `http://127.0.0.1:8000/metrics`.

## Shot list

| Time | Screen | Action | Say |
|---|---|---|---|
| 0:00–0:15 | Console (sam), Incident board | Show the header: 07:00, C0, kill switch off, mode normal. | "A simulated cold-chain fleet. Rules, the policy gate and the executor are deterministic code. No model decides whether anything runs." |
| 0:15–0:35 | sam, Replay | Press **+5 min** three times (07:15). Two reefer failures breach at about 07:10. | "Two trucks breach their 4 °C band. The engine opens incidents only from data that passed quality checks." |
| 0:35–0:50 | sam, Incident detail → `INC-S0007-00067-01` | Show the temperature line crossing the red band, the rule hits with the SOP clause, and the two L2 proposals. Under *Actions*: the ticket succeeded; the notification is `queued` after attempt 1, with an error. | "Two low-risk actions ran automatically within policy. The driver app is down, so the notification is being retried with backoff." |
| 0:50–1:05 | sam, Replay → **+30 min** (07:45), then Incident detail | The notification is now `failed` after 3 attempts. | "Three attempts, then it gives up. It does not keep hammering the service." |
| 1:05–1:25 | `/metrics` tab | Point at `coldchain_actions_total{status="retry_scheduled"…}`, `{status="failed"…}` and `coldchain_alerts_total{kind="action_failed"}`. | "Each failure is a metric and an alert, logged as JSON with the incident's correlation id." |
| 1:25–1:45 | Console (dana), Approval queue | One `escalate_tier2_manager` (L3) for the DEP-SD incident. The DEP-OXN incident's escalation is not shown, because it is not dana's depot. | "The engine flagged the incident and proposed a Tier 2 escalation. That is L3: it needs a named human. Dana only sees her depots." |
| 1:45–2:10 | dana, Approval queue | Type the reason "driver app down, calling manager", then **Approve**. Show the green "Gate: execute". | "Her approval is bound to the exact proposal she sees, its sha256. An edited or replayed approval is rejected." |
| 2:10–2:25 | sam, Replay → advance **5 min**, then Incident detail | The escalation shows `succeeded` under *Actions*. | "The executor checks the stored approval itself before it runs anything." |
| 2:25–2:45 | sam, Autonomy panel | Switch the **kill switch ON**, advance 5 min, and show that nothing new executes. Switch it off. | "A supervisor can stop all execution within one polling interval. That change is audited too." |
| 2:45–3:00 | Audit export (`/audit`, as sam) | Show `chain_valid: true` and the entries: `gate_decision`, `action_executing`, `action_failed`, `approval_recorded`, `kill_switch_set`. | "Every decision is in an append-only, hash-chained log. Simulated fleet; results in `eval/EVAL.md`." |

## Notes

- **Expected states, checked offline.** With `--fault driver.notify=fail`, starting at 07:00:
  - at 07:15 the DEP-SD notification is queued after 1 attempt, and its ticket has succeeded;
  - at 07:45 both notifications (DEP-SD and DEP-OXN) have failed after 3 attempts, and two escalations have waited since 07:25;
  - after dana approves at 07:45 and the clock moves 5 min, the escalation succeeds.
- **If an approval is rejected** as `stale_or_future_approval`, the approval is older than its 30-minute TTL. Approve before advancing the clock again.
- **Optional 15-second insert.** Show the console's Autonomy panel policy view, and say that an update lowering L3 to L2 is refused with HTTP 422 (`test_policy_update_cannot_lower_autonomy_level`).
- **GIF for the README.** Record the 0:50–2:25 segment as `docs/demo.gif`. This is not recorded yet.
