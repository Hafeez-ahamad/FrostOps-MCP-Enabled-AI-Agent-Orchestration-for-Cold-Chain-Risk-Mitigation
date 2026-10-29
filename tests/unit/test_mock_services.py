"""Mock services and the engine's clients: normal answers, idempotency, injected faults."""

from datetime import datetime, timedelta

import pytest

from engine.integrations.clients import Integrations, StaleCache
from sim.labels import WorldEvent
from sim.mock_services.faults import Fault, FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations

NOW = datetime(2026, 3, 2, 12, 0)


@pytest.fixture
def world() -> MockWorld:
    return MockWorld(now=NOW, port_congestion={"POLB": 3.2})


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector()


@pytest.fixture
def svc(world: MockWorld, faults: FaultInjector) -> Integrations:
    return in_process_integrations(world, faults)


def test_driver_notification_is_idempotent(svc: Integrations, world: MockWorld) -> None:
    first = svc.driver.notify("S1", "restart cooling", "INC-1:notify:S1")
    again = svc.driver.notify("S1", "restart cooling", "INC-1:notify:S1")
    assert first.ok and again.ok
    assert first.data == again.data
    assert len([e for e in world.log if e["service"] == "driver"]) == 1


def test_unreachable_driver_is_a_retryable_failure(svc: Integrations, world: MockWorld) -> None:
    world.events.append(
        WorldEvent(
            kind="driver_unreachable",
            subject_id="S1",
            start_ts=NOW,
            end_ts=NOW + timedelta(hours=1),
        )
    )
    result = svc.driver.notify("S1", "restart cooling", "k1")
    assert not result.ok and result.retryable and result.error == "driver_unreachable"
    assert svc.driver.reachable("S1").data["reachable"] is False
    world.now = NOW + timedelta(hours=2)
    assert svc.driver.notify("S1", "restart cooling", "k1").ok


def test_injected_failure_has_no_side_effect(
    svc: Integrations, world: MockWorld, faults: FaultInjector
) -> None:
    faults.queue("ticketing.create", Fault(mode="fail"))
    failed = svc.ticketing.create_ticket("INC-1", "S1", "breach", "k-ticket")
    assert not failed.ok and failed.retryable and failed.error == "http_503"
    assert world.log == []
    assert svc.ticketing.create_ticket("INC-1", "S1", "breach", "k-ticket").ok


def test_partial_response_is_retryable_and_the_retry_does_not_repeat_the_effect(
    svc: Integrations, world: MockWorld, faults: FaultInjector
) -> None:
    faults.queue("ticketing.create", Fault(mode="partial"))
    partial = svc.ticketing.create_ticket("INC-1", "S1", "breach", "k-p")
    assert not partial.ok and partial.retryable and partial.error == "partial_response"
    retry = svc.ticketing.create_ticket("INC-1", "S1", "breach", "k-p")
    assert retry.ok
    assert len([e for e in world.log if e["service"] == "ticketing"]) == 1


def test_slow_reply_beyond_the_timeout_is_a_timeout(
    svc: Integrations, faults: FaultInjector
) -> None:
    faults.queue("driver.notify", Fault(mode="slow", latency_ms=5000))
    result = svc.driver.notify("S1", "restart cooling", "k-slow")
    assert not result.ok and result.error == "timeout" and result.latency_ms == 5000


def test_diversion_reserves_a_slot_and_cancel_releases_it(svc: Integrations) -> None:
    before = {f["facility_id"]: f["available"] for f in svc.facilities.list().data["facilities"]}
    diversion = svc.tms.divert("S1", "CS-VENTURA", "k-div")
    after = {f["facility_id"]: f["available"] for f in svc.facilities.list().data["facilities"]}
    assert after["CS-VENTURA"] == before["CS-VENTURA"] - 1
    assert svc.tms.cancel(diversion.data["diversion_id"]).ok
    final = {f["facility_id"]: f["available"] for f in svc.facilities.list().data["facilities"]}
    assert final["CS-VENTURA"] == before["CS-VENTURA"]


def test_full_facility_rejects_diversion_without_retry(svc: Integrations, world: MockWorld) -> None:
    world.events.append(
        WorldEvent(
            kind="facility_full",
            subject_id="CS-COMMERCE",
            start_ts=NOW,
            end_ts=NOW + timedelta(hours=1),
        )
    )
    result = svc.tms.divert("S1", "CS-COMMERCE", "k-full")
    assert not result.ok and not result.retryable and result.error == "facility_full"


def test_stale_cache_serves_the_last_good_value_marked_stale(
    svc: Integrations, faults: FaultInjector
) -> None:
    cache = StaleCache()
    fresh, stale = cache.read("POLB", svc.congestion.get("POLB"))
    assert fresh is not None and fresh["level"] == 3.2 and not stale
    faults.queue("congestion.get", Fault(mode="fail"))
    cached, stale = cache.read("POLB", svc.congestion.get("POLB"))
    assert stale and cached == fresh
    nothing, stale = cache.read("POLA", svc.congestion.get("POLA"))
    assert nothing is None and stale


def test_weather_reports_the_simulated_conditions(svc: Integrations, world: MockWorld) -> None:
    world.ambient_c = 31.5
    assert svc.weather.get(33.8, -118.2).data["ambient_c"] == 31.5
