# Simulator and data-quality layer

Everything here is **simulated**. The fleet, depots, facilities and every number are invented. Coordinates are approximate Southern California city locations, used only to get realistic distances.

## Running it

```powershell
.venv\Scripts\python -m sim generate sim/scenarios/smoke.yaml --out eval/results/tmp/smoke
.venv\Scripts\python -m eval.dq_report      # writes eval/results/phase1_dq_detection.csv
.\tasks.ps1 sim                             # generate + ingest + data-quality runner (needs the DB)
```

The same scenario and seed always produce byte-identical output (`test_same_seed_gives_identical_output`). Four random streams are derived from the seed: schedule, incidents, noise and physics. Changing the noise plan therefore never changes the fleet underneath it.

## World

| Item | Source | Notes |
|---|---|---|
| 5 depots, 2 ports, 9 destinations, 4 cold-storage sites, 1 overflow depot | `sim/config/world.yaml` | Inland Empire Overflow Depot is the diversion target named in SOP §2 |
| Cargo bands | `policy/cargo_types.yaml` | FRESH 0–4 °C is from SOP §1; FROZEN and PHARMA are config assumptions |
| Fleet | scenario `vehicles` (50–200) | Each vehicle has one depot and one cargo type |

## Schedule

Each vehicle repeats a fixed cycle:
1. An outbound trip, loaded. It emits a reading every 5 minutes.
2. An empty return trip with no readings.
3. A 1 h dwell.
4. A 3 h slack buffer, larger than the maximum delay (180 min), so trips never overlap.

Trip time is the road distance (straight-line × 1.3) at 60 km/h. Each vehicle starts at a random point of its first cycle, so the fleet is in steady state from minute zero. About 30% of trips go to a port.

## Physics (`sim/physics.py`)

Newton's law of cooling, integrated in 1-minute steps:

- Reefer on: `dT/dt = (k_ins + k_cool)(S − T) + door · k_door (A − T)`. The controller cancels the insulation leak, so cargo holds at the setpoint S.
- Reefer off: `dT/dt = k_ins (A − T) + door · k_door (A − T)`. Cargo drifts toward ambient A at about 2–3 °C/h on a warm day.

The constants are k_ins = 0.002, k_cool = 0.06 and k_door = 0.02 per minute. They are illustrative, not calibrated.

Ambient air follows a daily sine wave (mean 20 °C, ±7 °C, peaking at 15:00), with inland depots 4 °C hotter and a random offset per day. The sensor adds Gaussian noise (σ = 0.08 °C) and rounds to 0.01 °C.

The other telemetry fields are documented formulas in `sim/generator.py`:

- **Delay probability:** a logistic in ETA delay, plus a congestion term for port-bound trips.
- **Risk class:** derived from route risk and delay probability.
- **Port congestion:**
  - A mean-reverting walk, capped at 6.8 without a seeded spike.
  - Above 7.0 during a spike.
  - Port-bound trips are slowed while their port is above 7.0.

By construction, clean data never crosses an SOP threshold. Every rule hit in the simulator comes from a labelled incident (`test_clean_readings_never_cross_sop_thresholds`).

## Seeded incidents (`sim/incidents.py`)

`at_hour` opens a 2-hour window. Each chosen shipment's incident starts at the first tick when it has driven at least 15 minutes and still has at least 2 hours of planned trip left. One incident per shipment.

| Type | What happens | Oracle-best action (`sim/oracle.py`) |
|---|---|---|
| reefer_failure | Unit stops; optional ETA delay ramp | ETA delay ≤ 60 min: `notify_driver_restart_cooling`. Over 60: `divert_to_cold_storage` (nearest site with room). SOP §1 |
| door_open | Door open 6–12 min, then recovery | If the band was breached: `create_incident_ticket`. Otherwise: `no_action` |
| route_delay_cascade | Several shipments from one depot gain 100–150 min delay and +3 route risk | `escalate_tier2_manager`. SOP §3 |
| port_congestion_spike | Port congestion 7.6–9.0 for the window. Every shipment that was actually on the road to that port during the window is affected; this is labelled after the run, from the real (delayed) timeline | `divert_to_overflow_depot` (Inland Empire Overflow). SOP §2 |
| driver_unreachable | Reefer failure, and the driver cannot be contacted | `escalate_tier2_manager` |
| facility_full | Nearest cold-storage site is full while a nearby reefer fails, with a 90 min delay | `divert_to_cold_storage` to the nearest site that is not full |

The oracle is a fixed mapping from the true incident type and state, not an optimiser (see `docs/decisions.md`). Labels also record the true breach time from the noise-free temperature.

## Seeded noise (`sim/noise.py`)

Glitches only hit incident-free shipments, inside the first 60% of the trip, after at least 5 clean readings.

| Type | Effect | Caught by |
|---|---|---|
| spike | One reading +10–20 °C | `spike` (suspect) |
| flatline | 8–12 readings stuck at one value | `flatline` (suspect) |
| dropout | 4–12 readings never sent | `freshness` (shipment-level, invalid) |
| duplicate | One reading sent twice | ingestion dedup and `duplicate` (invalid) |
| out_of_order | One reading arrives 15–30 min late | `out_of_order` (suspect) |
| unit_mixup | 6–12 readings in °F | `unit_mismatch` (invalid) |
| gps_jump | One position 0.5–1.0° off | `gps_jump` (suspect) or `out_of_region` (invalid) |
| clock_skew | 6–12 readings with the device clock 20–60 min ahead | `clock_skew` (suspect) |

## Data-quality layer (`engine/dq/`)

Each reading is assessed in receive order and gets `valid | suspect | invalid` (see the `engine/dq/checks.py` docstring). Only valid readings move the per-shipment reference used by the spike, unit and GPS checks. Thresholds are in `policy/data_quality.yaml`.

Results are stored in `ops.ReadingQuality` (one row per reading) and `ops.DataQualityFlags` (one row per failed check). `rpt.VW_LATEST_VALID_READING` returns each shipment's latest valid reading. Row-level security limits it by depot.

**Measured on simulated data:** `eval/results/phase1_dq_detection.csv` (scenario `dq_eval`, seeds 1–3, 10 glitches of each type per seed).

## Known limits

- **Flatline after a glitch.** If the first real reading after a flatline happens to equal the stuck value to 0.01 °C, it is flagged too. This happened once in about 72,000 clean readings (seed 2) and is reported in the CSV, not tuned away.
- **The database runner checks freshness only at the `now` it is given.** Dropouts are therefore caught per runner call, not per tick. The in-memory stream (`engine/dq/stream.py`) checks every tick; Phase 5 replay drives the runner tick by tick.
- **Physics, delays and congestion are illustrative, not calibrated.** Results depend on these assumptions and must be read as simulated.
- **The simulator is open-loop for now.** Actions cannot change the physics yet; closing the loop is part of Phase 3/5.
