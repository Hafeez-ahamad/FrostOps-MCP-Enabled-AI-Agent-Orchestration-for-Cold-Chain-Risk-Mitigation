"""Replay harness: run configurations over seeds and blocks, write one result file per run.

python -m eval.replay --campaign mvp --configs C0 C1 C2 C3 --seeds 1 2 3 --blocks 1
python -m eval.replay --campaign final --configs C0 C1 C2 C3 --seeds 1 2 3 4 5 --blocks 3
python -m eval.replay --campaign mvp --variant injection --configs C1 C2 C3 --seeds 1 2 3 --blocks 1

Each run (configuration x seed x block x variant) is written atomically to
eval/results/phase5/<campaign>/runs/<config>_<variant>_s<seed>_b<block>.json and skipped if it
already exists, so a long campaign can be stopped and resumed. `--llm scripted` uses the
scripted stand-in model for plumbing checks; its results are never reported. Then build the
tables with `python -m eval.report --campaign <campaign>`.
"""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from engine.actions.models import Approval, Proposal
from engine.data.memory_db import in_memory_engine
from engine.llm.factory import build_structured_llm
from engine.llm.fake import ResponderChatModel, heuristic_responder
from engine.llm.structured import LLMCallLog, StructuredLLM, TokenBudget
from engine.runtime import Configuration, Runtime
from engine.settings import PROJECT_ROOT, Settings, load_settings
from eval.injection import follows_injection, hostile_sop, hostile_world
from eval.metrics import TRUE_REEFER_FAILURE, incident_rows, run_row
from eval.redteam.c3_no_gate import remove_gate
from sim.generator import generate
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import load_scenario
from sim.world import load_world

RESULTS = PROJECT_ROOT / "eval" / "results" / "phase5"
SCENARIO = PROJECT_ROOT / "sim" / "scenarios" / "detection_eval.yaml"
APPROVAL_DELAY = timedelta(minutes=10)
CONFIGS = ("C0", "C1", "C2", "C3")


def simulated_dispatcher(delay: timedelta = APPROVAL_DELAY) -> Any:
    """Approves every pending L3 proposal `delay` after first seeing it. Never sees the oracle."""
    first_seen: dict[str, datetime] = {}

    def approve(pending: list[Proposal], now: datetime) -> list[Approval]:
        out = []
        for p in pending:
            seen = first_seen.setdefault(p.proposal_id, now)
            if now - seen >= delay:
                out.append(
                    Approval(
                        approval_id=f"APR-{p.proposal_id}",
                        proposal_id=p.proposal_id,
                        proposal_hash=p.content_hash(),
                        approver_id="simulated",
                        approver_role="dispatcher",
                        decision="approve",
                        decided_at=now,
                    )
                )
        return out

    return approve


def sim_seed(seed: int, block: int) -> int:
    return seed * 100 + block


def run_path(campaign: str, config: str, variant: str, seed: int, block: int) -> Path:
    return RESULTS / campaign / "runs" / f"{config}_{variant}_s{seed}_b{block}.json"


def build_llm(mode: str, settings: Settings, engine: Any, run_id: str) -> StructuredLLM:
    if mode == "scripted":
        return StructuredLLM(
            ResponderChatModel(responder=heuristic_responder),
            "scripted-fake",
            TokenBudget(settings.llm_token_budget_per_run),
            run_id,
            LLMCallLog(engine),
        )
    return build_structured_llm(settings, engine, run_id)


def run_one(campaign: str, config: str, variant: str, seed: int, block: int, llm_mode: str) -> Path:
    path = run_path(campaign, config, variant, seed, block)
    settings = load_settings()
    scenario = load_scenario(SCENARIO).with_seed(sim_seed(seed, block))
    sim = generate(scenario)
    world = hostile_world() if variant == "injection" else load_world()
    sop = hostile_sop(path.parent) if variant == "injection" else None
    mock = MockWorld(world=world, now=scenario.start, events=sim.events)
    engine = in_memory_engine()
    run_id = f"{campaign}-{config}-{variant}-s{seed}-b{block}"
    llm = None if config == "C0" else build_llm(llm_mode, settings, engine, run_id)
    graph_config: Configuration = "C2" if config == "C3" else config  # type: ignore[assignment]
    runtime = Runtime.build(
        engine,
        in_process_integrations(mock, FaultInjector()),
        config=graph_config,
        llm=llm,
        explain=False,
        sop=sop,
    )
    shadow = remove_gate(runtime).shadow if config == "C3" else None

    def on_tick(t: datetime) -> None:
        mock.now = t

    started = time.perf_counter()
    runtime.run(
        sim.readings.to_dict("records"),
        timedelta(minutes=scenario.reading_interval_min),
        scenario.start + timedelta(hours=scenario.duration_hours),
        on_tick=on_tick,
        approver=simulated_dispatcher(),
    )
    wall = time.perf_counter() - started
    key = {
        "campaign": campaign,
        "config": config,
        "variant": variant,
        "seed": seed,
        "block": block,
        "sim_seed": scenario.seed,
        "llm_mode": "none" if config == "C0" else llm_mode,
    }
    calls = llm.calls if llm is not None else []
    labels = {lb.shipment_id: lb.type for lb in sim.incidents}
    result = {
        "run": run_row(sim, runtime, key, calls, wall, shadow),
        "incidents": incident_rows(sim, world, runtime, key),
        "choices": [
            {
                **key,
                **{
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in c.__dict__.items()
                },
                "llm_involved": c.planner_kind == "llm" or c.verdict is not None,
                "followed_injection": follows_injection(
                    choice_index=c.choice_index,
                    candidate_count=c.candidate_count,
                    verdict=c.verdict,
                    true_reefer_failure=labels.get(c.shipment_id) in TRUE_REEFER_FAILURE,
                ),
            }
            for c in runtime.dispatcher.ctx.choices
        ],
        "llm_calls": [
            {
                **key,
                **{
                    k: (v.isoformat() if isinstance(v, datetime) else v)
                    for k, v in c.items()
                    if k not in ("Prompt", "Completion")
                },
            }
            for c in calls
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves half a result
    return path


def manifest(campaign: str, llm_mode: str) -> None:
    """Record what produced the campaign: git commit, model digest, Ollama version, settings."""
    settings = load_settings()
    info: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "llm_mode": llm_mode,
    }
    try:
        info["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        ).stdout.strip()
        info["git_dirty"] = bool(dirty)
    except (OSError, subprocess.CalledProcessError):
        info["git_commit"] = None
    if llm_mode == "ollama":
        try:
            tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=5).json()["models"]
            info["model"] = next(
                (
                    {"name": m["name"], "digest": m["digest"]}
                    for m in tags
                    if m["name"] == settings.ollama_model
                ),
                None,
            )
            info["ollama_version"] = httpx.get(
                "http://127.0.0.1:11434/api/version", timeout=5
            ).json()
        except (httpx.HTTPError, KeyError, ValueError):
            info["model"] = None
    info.update(
        {
            "temperature": 0,
            "llm_seed": settings.llm_seed,
            "token_budget_per_run": settings.llm_token_budget_per_run,
            "explainer": False,
            "scenario": SCENARIO.name,
            "approval_delay_min": APPROVAL_DELAY.total_seconds() / 60,
        }
    )
    path = RESULTS / campaign / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    runs = json.loads(path.read_text(encoding="utf-8")).get("launches", []) if path.exists() else []
    runs.append(info)
    path.write_text(json.dumps({"launches": runs}, indent=1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=CONFIGS)
    parser.add_argument("--seeds", nargs="+", type=int, default=[1, 2, 3])
    parser.add_argument("--blocks", type=int, default=1)
    parser.add_argument("--variant", choices=["normal", "injection"], default="normal")
    parser.add_argument("--llm", choices=["ollama", "scripted"], default="ollama")
    args = parser.parse_args()
    manifest(args.campaign, args.llm)
    for seed in args.seeds:
        for block in range(1, args.blocks + 1):
            for config in args.configs:
                path = run_path(args.campaign, config, args.variant, seed, block)
                if path.exists():
                    print(f"skip {path.name} (done)", flush=True)
                    continue
                started = time.perf_counter()
                run_one(args.campaign, config, args.variant, seed, block, args.llm)
                print(f"done {path.name} in {time.perf_counter() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
