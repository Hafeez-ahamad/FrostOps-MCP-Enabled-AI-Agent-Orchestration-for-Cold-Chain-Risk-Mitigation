"""Measure the data-quality layer on seeded glitches (simulated data).

python -m eval.dq_report [--scenario sim/scenarios/dq_eval.yaml] [--seeds 1 2 3]

Writes eval/results/phase1_dq_detection.csv with one row per (seed, metric):
- glitch:<type>      seeded glitches of that type, how many were flagged, recall
- clean_readings     readings with no glitch: how many were flagged (false flags)
- incident_readings  readings during a real incident: how many were flagged (hidden breaches)

A glitch counts as detected if any of its readings is not `valid` (for a dropout: if a
freshness flag fires for that shipment after the gap starts). All numbers are simulated.
"""

import argparse
import csv
from datetime import timedelta
from pathlib import Path

from engine.dq.checks import DataQualityChecker
from engine.dq.stream import assess_stream
from engine.settings import PROJECT_ROOT
from sim.generator import generate
from sim.scenario import Scenario, load_scenario

DEFAULT_SCENARIO = PROJECT_ROOT / "sim" / "scenarios" / "dq_eval.yaml"
OUTPUT = PROJECT_ROOT / "eval" / "results" / "phase1_dq_detection.csv"
FIELDS = ["scenario", "seed", "metric", "total", "flagged", "rate"]


def evaluate_seed(scenario: Scenario) -> list[dict[str, object]]:
    out = generate(scenario)
    end = scenario.start + timedelta(hours=scenario.duration_hours)
    tick = timedelta(minutes=scenario.reading_interval_min)
    result = assess_stream(out.readings.to_dict("records"), DataQualityChecker(), tick, end)
    status = {
        int(e): a.status.value
        for e, a in zip(out.readings["gt_emit_id"], result.assessments, strict=True)
    }

    rows: list[dict[str, object]] = []

    def add(metric: str, total: int, flagged: int) -> None:
        rate = round(flagged / total, 6) if total else ""
        rows.append(
            {
                "scenario": scenario.name,
                "seed": scenario.seed,
                "metric": metric,
                "total": total,
                "flagged": flagged,
                "rate": rate,
            }
        )

    for glitch_type in sorted({g.type for g in out.glitches}):
        glitches = [g for g in out.glitches if g.type == glitch_type]
        detected = 0
        for g in glitches:
            if g.type == "dropout":
                start = g.start_ts
                hit = start is not None and any(
                    f.shipment_id == g.shipment_id and f.evaluated_at >= start
                    for f in result.freshness_flags
                )
            else:
                hit = any(status[e] != "valid" for e in g.emit_ids)
            detected += hit
        add(f"glitch:{glitch_type}", len(glitches), detected)

    readings = out.readings
    clean = readings["gt_glitch_id"].isna()
    incident = readings["gt_incident_id"].notna() & clean
    flagged = [a.status.value != "valid" for a in result.assessments]
    add(
        "clean_readings", int(clean.sum()), sum(f for f, c in zip(flagged, clean, strict=True) if c)
    )
    add(
        "incident_readings",
        int(incident.sum()),
        sum(f for f, i in zip(flagged, incident, strict=True) if i),
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    args = parser.parse_args()
    base = load_scenario(args.scenario)
    rows = [row for seed in args.seeds for row in evaluate_seed(base.with_seed(seed))]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)} ({len(rows)} rows, seeds {args.seeds})")


if __name__ == "__main__":
    main()
