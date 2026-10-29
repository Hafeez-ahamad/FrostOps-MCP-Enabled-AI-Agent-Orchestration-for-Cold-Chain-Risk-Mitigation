"""Simulator command line.

python -m sim generate sim/scenarios/smoke.yaml --out eval/results/tmp/smoke [--seed N]
    Write readings.csv (legacy columns + gt_* ground truth) and label files. No database.
python -m sim load sim/scenarios/smoke.yaml [--seed N]
    Generate, ingest into the legacy table (append + dedup) and run the data-quality runner.
"""

import argparse
from datetime import timedelta
from pathlib import Path

from engine.data.ingest import ingest_readings
from engine.db import build_engine
from engine.dq.runner import assess_pending
from engine.settings import load_settings
from sim.generator import generate
from sim.scenario import load_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description="Cold-chain fleet simulator")
    parser.add_argument("command", choices=["generate", "load"])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--seed", type=int, default=None, help="override the scenario seed")
    parser.add_argument("--out", type=Path, default=None, help="output folder for generate")
    args = parser.parse_args()

    scenario = load_scenario(args.scenario)
    if args.seed is not None:
        scenario = scenario.with_seed(args.seed)
    output = generate(scenario)
    print(
        f"{scenario.name} seed={scenario.seed}: {len(output.readings)} readings, "
        f"{len(output.shipments)} shipments, {len(output.incidents)} incidents, "
        f"{len(output.glitches)} glitches (simulated)"
    )

    if args.command == "generate":
        out_dir = args.out or Path("eval/results/tmp") / f"{scenario.name}-seed{scenario.seed}"
        output.write(out_dir)
        print(f"wrote {out_dir}")
        return

    engine = build_engine(load_settings(), "ingest", fast_executemany=True)
    print(ingest_readings(engine, output.readings))
    end = scenario.start + timedelta(hours=scenario.duration_hours)
    print(assess_pending(engine, now=end))


if __name__ == "__main__":
    main()
