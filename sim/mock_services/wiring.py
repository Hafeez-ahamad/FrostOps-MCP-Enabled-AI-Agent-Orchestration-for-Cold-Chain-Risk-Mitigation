"""Connect the engine's integration clients to in-process mock apps (tests and replay).

FastAPI's TestClient is an httpx.Client that calls the app directly: no network, no ports.
"""

from fastapi.testclient import TestClient

from engine.integrations.clients import (
    CongestionClient,
    DriverClient,
    FacilitiesClient,
    Integrations,
    TicketingClient,
    TmsClient,
    WeatherClient,
)
from sim.mock_services.apps import create_all
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld


def in_process_integrations(
    world: MockWorld, faults: FaultInjector, timeout_ms: float = 2000.0
) -> Integrations:
    apps = create_all(world, faults)

    def http(name: str) -> TestClient:
        return TestClient(apps[name], raise_server_exceptions=True)

    return Integrations(
        driver=DriverClient(http("driver"), timeout_ms),
        ticketing=TicketingClient(http("ticketing"), timeout_ms),
        tms=TmsClient(http("tms"), timeout_ms),
        facilities=FacilitiesClient(http("facilities"), timeout_ms),
        weather=WeatherClient(http("weather"), timeout_ms),
        congestion=CongestionClient(http("congestion"), timeout_ms),
    )
