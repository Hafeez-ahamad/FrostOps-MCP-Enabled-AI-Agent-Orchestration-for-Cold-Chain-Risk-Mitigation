"""Phase 3 demo (simulated): two auto (L2) actions and an approval-gated (L3) action, end to end.

python -m eval.phase3_demo [--scenario sim/scenarios/smoke.yaml]

Runs the C0 loop (data quality -> rules -> incidents -> planner -> gate -> executor) over a
simulated day against the in-process mock services. A stand-in dispatcher ("demo") approves
every L3 proposal shortly after it is queued. Writes to eval/results/:

  phase3_demo_actions.csv   every action: level, status, attempts, who approved it
  phase3_demo_audit.csv     the hash-chained audit trail
  phase3_demo_summary.csv   counts, including unauthorised actions (checked from the database)
"""

import argparse
import csv
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from engine.actions.models import Approval, Proposal
from engine.actions.policy import ActionLevel
from engine.audit.log import verify_chain
from engine.data.memory_db import in_memory_engine
from engine.runtime import Runtime
from engine.settings import PROJECT_ROOT
from sim.generator import generate
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import load_scenario

RESULTS = PROJECT_ROOT / "eval" / "results"
DEFAULT_SCENARIO = PROJECT_ROOT / "sim" / "scenarios" / "smoke.yaml"


def demo_dispatcher(pending: list[Proposal], now: datetime) -> list[Approval]:
    return [
        Approval(
            approval_id=f"APR-{p.proposal_id}",
            proposal_id=p.proposal_id,
            proposal_hash=p.content_hash(),
            approver_id="demo",
            approver_role="dispatcher",
            decision="approve",
            decided_at=now,
        )
        for p in pending
    ]


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    args = parser.parse_args()
    scenario = load_scenario(args.scenario)
    sim = generate(scenario)
    world = MockWorld(now=scenario.start, events=sim.events)
    runtime = Runtime.build(in_memory_engine(), in_process_integrations(world, FaultInjector()))

    def on_tick(t: datetime) -> None:
        world.now = t

    runtime.run(
        sim.readings.to_dict("records"),
        timedelta(minutes=scenario.reading_interval_min),
        scenario.start + timedelta(hours=scenario.duration_hours),
        on_tick=on_tick,
        approver=demo_dispatcher,
    )

    gate = runtime.gate
    actions = runtime.executor.actions.all()
    unauthorised = 0
    action_rows: list[dict[str, object]] = []
    for a in actions:
        proposal = gate.proposals.get(a.proposal_id)
        approval = gate.approvals.for_proposal(a.proposal_id)
        authorised = (
            proposal is not None
            and proposal["Status"] == "approved"
            and proposal["ProposalHash"] == a.proposal_hash
        )
        if a.level is ActionLevel.L3:
            authorised = authorised and approval is not None and approval["Decision"] == "approve"
        unauthorised += not authorised
        action_rows.append(
            {
                "action_id": a.action_id,
                "incident_id": a.incident_id,
                "action": a.action,
                "target": a.target,
                "level": a.level.value,
                "status": a.status.value,
                "attempts": a.attempts,
                "approved_by": f"{approval['ApproverRole']}:{approval['ApproverID']}"
                if approval
                else "gate (auto)",
                "created_at": a.created_at.isoformat(),
            }
        )
    audit = gate.audit.entries()
    by_level = Counter((a.level.value, a.status.value) for a in actions)
    summary: list[dict[str, object]] = [
        {"metric": "scenario", "value": f"{scenario.name} seed {scenario.seed} (simulated)"},
        {"metric": "incidents", "value": len(runtime.manager.incidents)},
        {"metric": "gate_decisions", "value": len(runtime.dispatcher.decisions)},
        *(
            {"metric": f"actions_{level}_{status}", "value": n}
            for (level, status), n in sorted(by_level.items())
        ),
        {"metric": "unauthorised_actions", "value": unauthorised},
        {"metric": "audit_entries", "value": len(audit)},
        {"metric": "audit_chain_valid", "value": verify_chain(audit)},
    ]

    RESULTS.mkdir(parents=True, exist_ok=True)
    _write(RESULTS / "phase3_demo_actions.csv", action_rows)
    _write(
        RESULTS / "phase3_demo_audit.csv",
        [
            {
                k: row[k]
                for k in (
                    "AuditID",
                    "At",
                    "Actor",
                    "EventType",
                    "ProposalID",
                    "ActionID",
                    "Detail",
                    "RowHash",
                )
            }
            for row in audit
        ],
    )
    _write(RESULTS / "phase3_demo_summary.csv", summary)
    for row in summary:
        print(f"{row['metric']}: {row['value']}")


if __name__ == "__main__":
    main()
