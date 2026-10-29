"""Fixed, parameterised read queries over rpt.VW_LATEST_VALID_READING.

No LLM writes SQL here (docs/engineering-rules.md). The LLM can only choose parameter values;
the queries themselves are fixed here and row counts are capped.
Only shipments still en route are returned.
"""

from typing import Any

from sqlalchemy import Engine, select

from engine.data.schema import LATEST_VALID_READING

MAX_ROWS = 20
MAX_RADIUS_DEG = 5.0

LATEST = LATEST_VALID_READING.c


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_ROWS)


def fleet_snapshot(engine: Engine, limit: int = 10) -> list[dict[str, Any]]:
    query = (
        select(LATEST_VALID_READING)
        .where(LATEST.TripStatus == "E")
        .order_by(LATEST.ReadingTs.desc(), LATEST.ShipmentID)
        .limit(_clamp_limit(limit))
    )
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(query)]


def readings_near(
    engine: Engine,
    latitude: float,
    longitude: float,
    radius_deg: float = 0.5,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise ValueError("latitude/longitude out of range")
    if not 0.0 < radius_deg <= MAX_RADIUS_DEG:
        raise ValueError(f"radius_deg must be in (0, {MAX_RADIUS_DEG}]")
    query = (
        select(LATEST_VALID_READING)
        .where(LATEST.TripStatus == "E")
        .where(LATEST.Latitude.between(latitude - radius_deg, latitude + radius_deg))
        .where(LATEST.Longitude.between(longitude - radius_deg, longitude + radius_deg))
        .order_by(LATEST.ReadingTs.desc(), LATEST.ShipmentID)
        .limit(_clamp_limit(limit))
    )
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(query)]


def format_rows(rows: list[dict[str, Any]]) -> str:
    """Render rows as a delimited data block for the LLM."""
    if not rows:
        return "<telemetry_rows>\nNo records matched.\n</telemetry_rows>"
    columns = list(rows[0])
    lines = [" | ".join(columns)]
    lines += [" | ".join(str(row[c]) for c in columns) for row in rows]
    return "<telemetry_rows>\n" + "\n".join(lines) + "\n</telemetry_rows>"
