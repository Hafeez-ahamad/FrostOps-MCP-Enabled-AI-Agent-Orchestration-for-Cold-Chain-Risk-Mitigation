from typing import Any

import pytest
import requests

from engine.tools.corridor import get_corridor_conditions


def test_disabled_source_reports_unavailable_without_network() -> None:
    result = get_corridor_conditions(33.77, -118.19, fetch=None)
    assert not result.available
    assert "disabled" in result.as_text()


def test_weather_is_reported_as_weather_and_never_as_congestion() -> None:
    def fake_fetch(lat: float, lon: float) -> dict[str, Any]:
        return {"temperature": 21.0, "windspeed": 15.0}

    result = get_corridor_conditions(33.77, -118.19, fetch=fake_fetch)
    text = result.as_text()

    assert (result.ambient_temp_c, result.wind_speed_kmh) == (21.0, 15.0)
    assert "wind_speed_kmh: 15.0" in text
    assert "not port congestion" in text
    assert "Congestion Index" not in text
    assert not hasattr(result, "congestion_index")


@pytest.mark.parametrize(
    "failure",
    [requests.ConnectionError("down"), KeyError("windspeed"), ValueError("bad")],
)
def test_source_errors_degrade_to_unavailable(failure: Exception) -> None:
    def broken(lat: float, lon: float) -> dict[str, Any]:
        raise failure

    result = get_corridor_conditions(33.77, -118.19, fetch=broken)
    assert not result.available
    assert result.wind_speed_kmh is None


def test_live_adapter_is_off_by_default(settings) -> None:  # type: ignore[no-untyped-def]
    assert settings.corridor_live_enabled is False
