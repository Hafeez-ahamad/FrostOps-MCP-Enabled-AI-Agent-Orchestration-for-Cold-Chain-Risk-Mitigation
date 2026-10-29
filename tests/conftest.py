from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings

from engine.dq.checks import DataQualityChecker
from engine.dq.stream import StreamResult, assess_stream
from engine.settings import PROJECT_ROOT, Settings
from sim.generator import SimulationOutput, generate
from sim.scenario import Scenario, load_scenario

SMOKE_PATH: Path = PROJECT_ROOT / "sim" / "scenarios" / "smoke.yaml"

# Same examples and properties as the default profile, without wall-clock checks: on a loaded
# 8 GB machine input generation and examples can be slow without anything being wrong.
hypothesis_settings.register_profile(
    "coldchain", deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
hypothesis_settings.load_profile("coldchain")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Defaults only: ignores any local .env so tests never depend on the developer's machine.
    Chat memory goes to a temporary folder."""
    return Settings(_env_file=None, chat_checkpoint_path=tmp_path / "chat.sqlite")  # type: ignore[call-arg]


@pytest.fixture(scope="session")
def smoke_scenario() -> Scenario:
    return load_scenario(SMOKE_PATH)


@pytest.fixture(scope="session")
def smoke(smoke_scenario: Scenario) -> SimulationOutput:
    """The smoke scenario, generated once per test session (about 3 s)."""
    return generate(smoke_scenario)


@pytest.fixture(scope="session")
def smoke_dq(smoke: SimulationOutput, smoke_scenario: Scenario) -> StreamResult:
    end = smoke_scenario.start + timedelta(hours=smoke_scenario.duration_hours)
    tick = timedelta(minutes=smoke_scenario.reading_interval_min)
    return assess_stream(smoke.readings.to_dict("records"), DataQualityChecker(), tick, end)
