"""Run the API on a simulated day (local demo):

python -m api [--scenario sim/scenarios/smoke.yaml] [--config C0] [--start-hours 6] [--port 8000]
              [--fault driver.notify=fail ...]

Builds an in-memory database, simulates the scenario, advances the clock `--start-hours` so there
is something to look at, and serves the API on 127.0.0.1. Without API_USERS it creates four demo
users (dispatcher, supervisor, auditor, admin) with random tokens and prints them once.
C1/C2 use the configured LLM (local Ollama by default); C0 needs none. `--fault` sets a standing
fault on a mock endpoint (modes: fail, slow, partial) for failure demos (docs/demo-script.md).
"""

import argparse
import os
from pathlib import Path
from typing import Any

import uvicorn

from api.app import ChatFn, create_app
from api.auth import User, UserDirectory, demo_directory
from api.service import EngineService
from engine.chat.service import create_chat_agent
from engine.chat.stream import run_chat_turn
from engine.data.memory_db import in_memory_engine
from engine.llm.factory import build_structured_llm
from engine.observability import configure_logging
from engine.runtime import Configuration, Runtime
from engine.settings import PROJECT_ROOT, load_settings
from sim.generator import generate
from sim.mock_services.faults import Fault, FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import load_scenario


def parse_faults(specs: list[str]) -> FaultInjector:
    """`endpoint=mode` pairs, e.g. driver.notify=fail, as standing faults."""
    faults = FaultInjector()
    for spec in specs:
        endpoint, _, mode = spec.partition("=")
        faults.set(endpoint, Fault(mode=mode, latency_ms=60_000 if mode == "slow" else 0.0))  # type: ignore[arg-type]
    return faults


def build_service(
    scenario_path: Path,
    config: Configuration,
    start_hours: float,
    faults: FaultInjector | None = None,
) -> EngineService:
    settings = load_settings()
    scenario = load_scenario(scenario_path)
    sim = generate(scenario)
    mock = MockWorld(now=scenario.start, events=sim.events)
    engine = in_memory_engine()
    llm = None if config == "C0" else build_structured_llm(settings, engine, f"api-{config}")
    runtime = Runtime.build(
        engine, in_process_integrations(mock, faults or FaultInjector()), config=config, llm=llm
    )
    service = EngineService(runtime, sim, mock, scenario.start)
    service.advance(start_hours * 60)
    return service


def chat_for(service: EngineService) -> ChatFn:
    agent: dict[str, Any] = {}

    def chat(message: str, thread_id: str, user: User) -> str:
        if "agent" not in agent:
            agent["agent"] = create_chat_agent(
                load_settings(), engine_factory=lambda: service.engine
            )
        answer = ""
        for event in run_chat_turn(agent["agent"], message, thread_id):
            if event.kind == "answer":
                answer = event.content
        return answer or "No answer."

    return chat


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", type=Path, default=PROJECT_ROOT / "sim" / "scenarios" / "smoke.yaml"
    )
    parser.add_argument("--config", choices=["C0", "C1", "C2"], default="C0")
    parser.add_argument("--start-hours", type=float, default=10.0)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--fault", action="append", default=[], metavar="ENDPOINT=MODE")
    args = parser.parse_args()
    configure_logging()

    if os.getenv("API_USERS"):
        users = UserDirectory.from_env(os.environ["API_USERS"])
    else:
        users, tokens = demo_directory()
        print("Demo users (tokens are shown once and not stored):")
        for user_id, token in tokens.items():
            print(f"  {user_id:5s} {token}")
    service = build_service(args.scenario, args.config, args.start_hours, parse_faults(args.fault))
    app = create_app(service, users, chat_for(service))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
