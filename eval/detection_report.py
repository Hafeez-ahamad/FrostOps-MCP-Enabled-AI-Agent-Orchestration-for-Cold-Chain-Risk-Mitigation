"""Measure detection (data quality -> SOP rules -> incidents) on seeded incidents (simulated).

python -m eval.detection_report [--scenario sim/scenarios/detection_eval.yaml] [--seeds 1 2 3]

Writes two CSVs to eval/results/:

phase2_detection.csv, one row per (seed, incident type) plus an `all` row:
    labelled     seeded incidents of that type
    expected     those that should be detected (all except door-open events that never
                 breached the band, whose correct outcome is no incident)
    detected     expected incidents with an incident opened on that shipment at or after the
                 true start
    recall       detected / expected
    ttd_p50_min, ttd_p95_min   minutes from true start to incident opened
    breach_ttd_p50_min         minutes from the true band breach to incident opened
                               (temperature incident types only)

phase2_false_alarms.csv, one row per seed:
    shipments, incidents_opened, true_incidents, false_alarms, precision
    false_alarms_per_100_shipments
    glitch_threshold_readings  glitched readings that would cross an SOP threshold if trusted
    glitch_threshold_blocked   how many of those data quality stopped (not `valid`)
    suspect_hits               rule hits kept for triage instead of opening incidents

Every number is from the simulator and must be labelled simulated.
"""

import argparse
import csv
from datetime import timedelta
from pathlib import Path

import numpy as np

from engine.detection import detect_stream
from engine.rules.engine import RuleInput, RulesEngine
from engine.settings import PROJECT_ROOT
from sim.generator import generate
from sim.scenario import Scenario, load_scenario

DEFAULT_SCENARIO = PROJECT_ROOT / "sim" / "scenarios" / "detection_eval.yaml"
RESULTS = PROJECT_ROOT / "eval" / "results"
DETECTION_CSV = RESULTS / "phase2_detection.csv"
FALSE_ALARM_CSV = RESULTS / "phase2_false_alarms.csv"
TEMPERATURE_TYPES = {"reefer_failure", "driver_unreachable", "facility_full", "door_open"}

DETECTION_FIELDS = [
    "scenario",
    "seed",
    "incident_type",
    "labelled",
    "expected",
    "detected",
    "recall",
    "ttd_p50_min",
    "ttd_p95_min",
    "breach_ttd_p50_min",
]
FALSE_ALARM_FIELDS = [
    "scenario",
    "seed",
    "shipments",
    "incidents_opened",
    "true_incidents",
    "false_alarms",
    "precision",
    "false_alarms_per_100_shipments",
    "glitch_threshold_readings",
    "glitch_threshold_blocked",
    "suspect_hits",
]


def _pct(values: list[float], q: float) -> float | str:
    return round(float(np.percentile(values, q)), 1) if values else ""


def evaluate_seed(scenario: Scenario) -> tuple[list[dict[str, object]], dict[str, object]]:
    out = generate(scenario)
    rows = out.readings.to_dict("records")
    end = scenario.start + timedelta(hours=scenario.duration_hours)
    result = detect_stream(rows, timedelta(minutes=scenario.reading_interval_min), end)
    incidents = list(result.manager.incidents.values())

    expected_labels = [lb for lb in out.incidents if lb.oracle_action != "no_action"]
    expected_shipments = {lb.shipment_id for lb in expected_labels}

    detection_rows: list[dict[str, object]] = []
    types = sorted({lb.type for lb in out.incidents})
    for incident_type in [*types, "all"]:
        labels = [lb for lb in out.incidents if incident_type in ("all", lb.type)]
        expected = [lb for lb in labels if lb.oracle_action != "no_action"]
        ttd: list[float] = []
        breach_ttd: list[float] = []
        for lb in expected:
            opened = sorted(
                i.opened_at
                for i in incidents
                if i.shipment_id == lb.shipment_id and i.opened_at >= lb.start_ts
            )
            if not opened:
                continue
            ttd.append((opened[0] - lb.start_ts).total_seconds() / 60)
            if lb.type in TEMPERATURE_TYPES and lb.true_breach_ts is not None:
                breach_ttd.append((opened[0] - lb.true_breach_ts).total_seconds() / 60)
        detection_rows.append(
            {
                "scenario": scenario.name,
                "seed": scenario.seed,
                "incident_type": incident_type,
                "labelled": len(labels),
                "expected": len(expected),
                "detected": len(ttd),
                "recall": round(len(ttd) / len(expected), 4) if expected else "",
                "ttd_p50_min": _pct(ttd, 50),
                "ttd_p95_min": _pct(ttd, 95),
                "breach_ttd_p50_min": _pct(breach_ttd, 50),
            }
        )

    true_incidents = [i for i in incidents if i.shipment_id in expected_shipments]
    false_alarms = len(incidents) - len(true_incidents)

    rules = RulesEngine()
    would_hit = blocked = 0
    for row, assessment in zip(rows, result.assessments, strict=True):
        if not isinstance(row["gt_glitch_id"], str):
            continue
        if rules.evaluate(RuleInput.from_legacy(row), "valid"):
            would_hit += 1
            blocked += assessment.status.value != "valid"

    shipments = len(out.shipments)
    summary: dict[str, object] = {
        "scenario": scenario.name,
        "seed": scenario.seed,
        "shipments": shipments,
        "incidents_opened": len(incidents),
        "true_incidents": len(true_incidents),
        "false_alarms": false_alarms,
        "precision": round(len(true_incidents) / len(incidents), 4) if incidents else "",
        "false_alarms_per_100_shipments": round(100 * false_alarms / shipments, 3),
        "glitch_threshold_readings": would_hit,
        "glitch_threshold_blocked": blocked,
        "suspect_hits": len(result.manager.suspect_hits),
    }
    return detection_rows, summary


def _write(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    args = parser.parse_args()
    base = load_scenario(args.scenario)
    detection: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    for seed in args.seeds:
        rows, summary = evaluate_seed(base.with_seed(seed))
        detection.extend(rows)
        summaries.append(summary)
    RESULTS.mkdir(parents=True, exist_ok=True)
    _write(DETECTION_CSV, DETECTION_FIELDS, detection)
    _write(FALSE_ALARM_CSV, FALSE_ALARM_FIELDS, summaries)
    print(f"wrote {DETECTION_CSV.name} and {FALSE_ALARM_CSV.name} (seeds {args.seeds})")


if __name__ == "__main__":
    main()
