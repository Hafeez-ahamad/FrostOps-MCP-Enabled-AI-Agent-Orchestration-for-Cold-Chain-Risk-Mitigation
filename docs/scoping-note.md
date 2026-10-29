# Scoping note

How this engine would be scoped with a real operator (written as a forward-deployed engineer would). Everything built so far runs on a **simulated** fleet; this note lists what must be learned and agreed before it could touch real operations.

## Problem statement (to confirm with the operator)

Refrigerated loads are lost when a reefer fails, a trip is delayed or a port is congested. Dispatchers apply the response SOP by hand, across many trucks, using noisy legacy telemetry. The goal is to:
- detect breaches reliably;
- automate the low-risk, reversible responses within strict limits;
- bring every high-impact decision to a named person with the evidence attached.

## Discovery questions

**Operations**
1. Which SOP is authoritative? Who owns it, and how are changes approved?
2. Which actions do dispatchers take today, how fast, and which would they trust a system to take alone?
3. What does a diversion actually cost: slot fees, delay penalties, re-delivery? Who can approve one, and how quickly at night?
4. How are drivers reached (app, phone, telematics)? How often are they unreachable?

**Data**
1. Which system is the source of truth for telemetry? How late, duplicated or wrong is it in practice? Which sensors are known to drift?
2. Are there shipment, vehicle and depot ids in the legacy feed, or do they need joining from the TMS?
3. What history exists with known outcomes (spoilage claims, rejected loads) to validate thresholds?

**Integration and security**
1. Which systems can the engine write to (ticketing, TMS, paging)? With what service accounts? Is there a sandbox?
2. Where must logs and audit records live, and for how long?
3. Which identity provider issues dispatcher and supervisor identities?

## Assumptions made for this build (all to validate)

- **SOP:** the SOP in `data/policy/` is complete for the three scenarios it covers.
- **Cargo bands:** the FRESH band is from the SOP; FROZEN and PHARMA bands are assumptions.
- **Reversibility:** notifying a driver and opening a ticket are low-risk and reversible; diversions and escalations need a person.
- **Effect timings:** cooling is back 10 min after the driver is notified, 60 min after a Tier 2 escalation, and on arrival after a diversion.
- **Approval time:** a dispatcher answers an approval request within 10 minutes.

## Success metrics (proposed)

| Metric | Proposed target | How it would be measured |
|---|---|---|
| Breach detection recall | ≥ 0.95 on labelled history | replay of historical incidents |
| False alarms | < 1 per 100 shipment-days | replay; dispatcher feedback |
| Unauthorised actions | 0 | audit log vs approvals (automated check) |
| Time to first action on a confirmed breach | median below today's manual baseline | audit log timestamps |
| Approval requests per dispatcher shift | agreed with operations (workload cap) | approval queue metrics |
| Cargo exposure (degree-minutes above band) | lower than today | telemetry after the pilot |

The simulated values for these metrics are in `eval/EVAL.md`. They show that the pipeline works, not that it would reach these targets on a real fleet.

## Out of scope for the pilot

- Fully autonomous diversions, load rejection, or any spend above a cap. These stay L3/L4.
- Multi-region and multi-tenant deployment, a React console, and an ML risk model (roadmap).

## Rollout path (proposed)

1. **Shadow mode** (`dry_run: true`): every decision is computed and audited; nothing executes. Compare with what dispatchers actually did.
2. **L2 only, one depot:** notifications and tickets. L3 still always goes to a human.
3. **More depots, with tuned limits.** Revisit autonomy levels only with evidence from the audit log.
