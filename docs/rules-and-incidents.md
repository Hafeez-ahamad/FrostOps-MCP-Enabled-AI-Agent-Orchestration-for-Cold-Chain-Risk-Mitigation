# Rules, risk and incidents (Phase 2)

Detection is deterministic code with no LLM anywhere in it. A test (`test_deterministic_core_imports_no_llm_library`) fails if any LLM library is imported in `engine/rules`, `engine/risk`, `engine/incidents`, `engine/dq` or `engine/detection.py`.

```
reading ──► data quality ──► SOP rules ──► incidents ──► (Phase 3: planner → gate → executor)
            valid/suspect/   R-TEMP-01 …   open / merge /
            invalid          (strict >)    clear, risk score
```

## SOP rules (`policy/sop_rules.yaml`, `engine/rules/engine.py`)

| Rule | Condition (strict) | SOP |
|---|---|---|
| R-TEMP-01 | temperature > band max for the cargo type (FRESH 4.0 °C; FROZEN/PHARMA from config) | §1 |
| R-CONG-01 | port congestion > 7.0 (no value = not port-bound = no hit) | §2 |
| R-RISK-01 | risk class "High Risk" and delay probability > 0.65 | §3 |
| R-ETA-01 | R-TEMP-01 on the same reading and ETA delay > 60 min | §1 mitigation |

**Traceability.**
- Each rule in the YAML quotes the SOP sentence it implements.
- A test checks that the quote appears verbatim in that clause, and that the YAML thresholds equal the SOP's.

**Boundary tests.**
- Each threshold is tested on both sides: 4.0 vs 4.01, 7.0 vs 7.01, 0.65 vs 0.66, and 60 vs 60.1 minutes.
- Hypothesis property tests check that every rule is exactly its strict comparison over the whole input range.

**Data quality.**
- Invalid readings are never evaluated.
- Hits on suspect readings are kept for triage but never open an incident.
- Only hits on valid readings change incidents.

Each hit records:
- the rule id;
- the observed values and the threshold;
- the reading's data-quality status;
- the SOP reference.

## Risk score (`engine/risk/score.py`)

`risk = 100 × severity × duration × exposure × sensitivity`. Each factor is in [0, 1]. The docstring gives the exact definitions.

- **Severity:** how far past the threshold the worst active rule is. It is floored at 0.2 so any confirmed hit counts.
- **Duration:** minutes active ÷ 60, between 0.1 and 1.
- **Exposure:** 0.5 + 0.5 × min(1, ETA delay ÷ 120). The legacy telemetry has no remaining time to delivery, so ETA delay stands in for it until the TMS mock exists.
- **Sensitivity:** a per-cargo weight from `policy/cargo_types.yaml` (a configuration assumption).

The score only ranks incidents for the dispatcher. It never decides whether an action runs. Property tests check that it stays between 0 and 100 and never decreases as a factor gets worse. Two anchor values are tested: 3.5 and 100.

## Incidents (`engine/incidents/manager.py`)

- **Open:** a valid hit on a shipment with no open incident opens `INC-<shipment>-<n>`. The id is deterministic, so a replay gets the same ids.
- **Merge:** further valid hits on that shipment update the same incident:
  - the rules seen;
  - the hit count;
  - the latest evidence;
  - the risk score.
- **One open incident per shipment:** the code enforces this, and so does a filtered unique index in SQL Server (`UX_Incidents_OpenPerShipment`).
- **Clear:** after 30 minutes of valid readings with no hits, the incident is cleared. A later hit opens a new one. Suspect or invalid readings can neither clear nor open an incident.
- **Storage:**
  - `ops.RuleHits` is append-only.
  - `ops.Incidents` is updated in place by id and never deleted.
  - Code: `engine/incidents/store.py`.

## Measured on simulated data

Produced by `python -m eval.detection_report`: scenario `detection_eval`, seeds 1–3, each two simulated days with 150 vehicles. All numbers are **simulated**.

- **Recall per incident type and time-to-detect:** [`eval/results/phase2_detection.csv`](../eval/results/phase2_detection.csv).
- **False alarms, precision, glitched readings stopped by data quality:** [`eval/results/phase2_false_alarms.csv`](../eval/results/phase2_false_alarms.csv).

How to read the numbers:
- **Door-open events that never breached are not "expected".** For them, the correct outcome is no incident.
- **Time-to-detect** runs from the true start of the incident. For a reefer failure that includes the time the cargo takes to warm past 4 °C.
- **Breach time-to-detect** runs from the true band breach, so it measures the detector's own delay.
- **Negative breach times are real, not errors.** Another rule can fire before the temperature crosses: for example R-RISK-01 from a 90-minute delay, as in `facility_full`. Sensor noise can also cross 4.0 one reading before the noise-free temperature does.

## Known limits

- The simulator keeps clean data below every threshold (see `docs/simulator.md`), so precision here measures the pipeline's logic, not real-world sensor behaviour.
- Ground truth matters. A first run showed 2 "false alarms" that were really trucks the simulator had failed to label: they were delayed into a port spike after it had been scheduled. The labelling was fixed, not the detector; the regression test is `test_ground_truth_covers_delayed_shipments_drifting_into_a_port_spike`.
- Incidents are built in memory and stored with `engine/incidents/store.py`. A database-driven incremental detection job and the monitor's own login come with the event queue (Phase 4) and per-agent roles (Phase 7).
