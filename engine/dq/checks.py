"""Data-quality checks: run on every reading before any SOP rule sees it.

Deterministic, no LLM. Each reading gets `valid | suspect | invalid`:
- invalid: the value cannot be right (duplicate, missing, out of range, wrong unit, outside the
  operating region). It is never used.
- suspect: it might be right but cannot be trusted on its own (spike, flatline, GPS jump,
  clock skew, out of order). Rules ignore it; the monitor may triage it.
- valid: passed every check. Only valid readings update the per-shipment reference values.

Checks are applied in receive order. Thresholds come from policy/data_quality.yaml.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from engine.geo import haversine_km
from engine.settings import PROJECT_ROOT

DQ_CONFIG_PATH = PROJECT_ROOT / "policy" / "data_quality.yaml"
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class DqStatus(StrEnum):
    VALID = "valid"
    SUSPECT = "suspect"
    INVALID = "invalid"


_SEVERITY = {DqStatus.VALID: 0, DqStatus.SUSPECT: 1, DqStatus.INVALID: 2}


class RegionBox(BaseModel, frozen=True):
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


class DqConfig(BaseModel, frozen=True):
    freshness_minutes: float
    temp_range_c: tuple[float, float]
    region_bbox: RegionBox
    max_temp_rate_c_per_min: float
    spike_margin_c: float
    unit_min_jump_c: float
    unit_tolerance_c: float
    flatline_run: int
    max_speed_kmh: float
    clock_skew_tolerance_s: float


def load_dq_config(path: Path = DQ_CONFIG_PATH) -> DqConfig:
    return DqConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


def parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if number != number else number  # NaN -> None


@dataclass(frozen=True)
class Reading:
    shipment_id: str
    seq: int
    ts: datetime | None  # device time
    received: datetime | None
    lat: float | None
    lon: float | None
    temp_c: float | None
    trip_status: str | None

    @classmethod
    def from_legacy(cls, row: Mapping[Any, Any]) -> "Reading":
        return cls(
            shipment_id=str(row["CNSG_NO"]),
            seq=int(row["PKT_SEQ"]),
            ts=parse_ts(row["EVT_DTTM"]),
            received=parse_ts(row["RCPT_DTTM"]),
            lat=_num(row["GPS_Y"]),
            lon=_num(row["GPS_X"]),
            temp_c=_num(row["RFR_TMP_C"]),
            trip_status=row.get("LEG_STAT"),
        )


@dataclass(frozen=True)
class DqFlag:
    shipment_id: str
    seq: int | None  # None for shipment-level flags (freshness)
    check: str
    status: DqStatus
    detail: str
    evaluated_at: datetime


@dataclass(frozen=True)
class Assessment:
    shipment_id: str
    seq: int
    status: DqStatus
    flags: tuple[DqFlag, ...]


@dataclass
class _ShipmentState:
    seen: set[int] = field(default_factory=set)
    max_seq: int = 0
    last_received: datetime | None = None
    en_route: bool = True
    stale_flagged: bool = False
    ref_temp: float | None = None  # last valid reading
    ref_ts: datetime | None = None
    ref_lat: float | None = None
    ref_lon: float | None = None
    flat_value: float | None = None
    flat_run: int = 0


class DataQualityChecker:
    def __init__(self, config: DqConfig | None = None) -> None:
        self.config = config or load_dq_config()
        self._state: dict[str, _ShipmentState] = {}

    def warm_start(
        self,
        shipment_id: str,
        *,
        max_seq: int,
        last_received: datetime | None,
        en_route: bool,
        reference: Reading | None,
    ) -> None:
        """Restore what a previous run knew (used by the database runner).

        `seen` is not restored: the database's unique key already rejects duplicates, and a
        late reading that fills a gap must be judged out of order, not duplicate.
        `reference` is the latest valid reading.
        """
        st = self._state.setdefault(shipment_id, _ShipmentState())
        st.max_seq = max(st.max_seq, max_seq)
        st.last_received = last_received
        st.en_route = en_route
        if reference is not None:
            st.ref_temp, st.ref_ts = reference.temp_c, reference.ts
            st.ref_lat, st.ref_lon = reference.lat, reference.lon

    def assess(self, r: Reading, now: datetime | None = None) -> Assessment:
        cfg = self.config
        evaluated_at = now or r.received or datetime.min
        st = self._state.setdefault(r.shipment_id, _ShipmentState())
        flags: list[DqFlag] = []

        def flag(check: str, status: DqStatus, detail: str) -> None:
            flags.append(DqFlag(r.shipment_id, r.seq, check, status, detail, evaluated_at))

        def result() -> Assessment:
            worst = max(
                (f.status for f in flags), key=_SEVERITY.__getitem__, default=DqStatus.VALID
            )
            return Assessment(r.shipment_id, r.seq, worst, tuple(flags))

        if r.seq in st.seen:
            flag("duplicate", DqStatus.INVALID, f"sequence {r.seq} already received")
            return result()
        st.seen.add(r.seq)
        if r.received is not None:
            st.last_received = r.received
        st.stale_flagged = False
        st.en_route = r.trip_status != "A"

        if None in (r.ts, r.received, r.lat, r.lon, r.temp_c):
            flag("missing_value", DqStatus.INVALID, "timestamp, position or temperature missing")
            return result()
        assert r.ts is not None and r.received is not None
        assert r.lat is not None and r.lon is not None and r.temp_c is not None

        low, high = cfg.temp_range_c
        if not low <= r.temp_c <= high:
            flag(
                "out_of_range", DqStatus.INVALID, f"temperature {r.temp_c} outside [{low}, {high}]"
            )
        if not cfg.region_bbox.contains(r.lat, r.lon):
            flag("out_of_region", DqStatus.INVALID, f"position {r.lat}, {r.lon} outside the region")

        timing_ok = True
        ahead_s = (r.ts - r.received).total_seconds()
        if ahead_s > cfg.clock_skew_tolerance_s:
            flag(
                "clock_skew", DqStatus.SUSPECT, f"device clock {ahead_s:.0f}s ahead of receive time"
            )
            timing_ok = False
        if r.seq < st.max_seq:
            flag("out_of_order", DqStatus.SUSPECT, f"sequence {r.seq} arrived after {st.max_seq}")
            timing_ok = False
        st.max_seq = max(st.max_seq, r.seq)

        if st.ref_temp is not None and st.ref_ts is not None:
            jump = abs(r.temp_c - st.ref_temp)
            as_celsius = (r.temp_c - 32.0) / 1.8
            unit_mixup = (
                jump > cfg.unit_min_jump_c and abs(as_celsius - st.ref_temp) <= cfg.unit_tolerance_c
            )
            if unit_mixup:
                flag("unit_mismatch", DqStatus.INVALID, f"{r.temp_c} looks like Fahrenheit")
            elif timing_ok:
                minutes = max(0.0, (r.ts - st.ref_ts).total_seconds() / 60)
                allowed = cfg.max_temp_rate_c_per_min * minutes + cfg.spike_margin_c
                if jump > allowed:
                    flag("spike", DqStatus.SUSPECT, f"changed {jump:.2f} C in {minutes:.0f} min")
            if timing_ok and st.ref_lat is not None and st.ref_lon is not None:
                km = haversine_km(st.ref_lat, st.ref_lon, r.lat, r.lon)
                hours = (r.ts - st.ref_ts).total_seconds() / 3600
                if (hours > 0 and km / hours > cfg.max_speed_kmh) or (hours <= 0 and km > 1.0):
                    flag("gps_jump", DqStatus.SUSPECT, f"moved {km:.0f} km in {hours * 60:.0f} min")

        if r.temp_c == st.flat_value:
            st.flat_run += 1
        else:
            st.flat_value, st.flat_run = r.temp_c, 1
        if st.flat_run >= cfg.flatline_run:
            flag("flatline", DqStatus.SUSPECT, f"{st.flat_run} identical readings")

        outcome = result()
        if outcome.status is DqStatus.VALID:
            st.ref_temp, st.ref_ts, st.ref_lat, st.ref_lon = r.temp_c, r.ts, r.lat, r.lon
        return outcome

    def check_freshness(self, now: datetime) -> list[DqFlag]:
        """One `stale` flag per gap for each en-route shipment that has gone quiet."""
        limit = timedelta(minutes=self.config.freshness_minutes)
        flags = []
        for shipment_id, st in sorted(self._state.items()):
            if not st.en_route or st.stale_flagged or st.last_received is None:
                continue
            if now - st.last_received > limit:
                st.stale_flagged = True
                minutes = (now - st.last_received).total_seconds() / 60
                flags.append(
                    DqFlag(
                        shipment_id,
                        None,
                        "freshness",
                        DqStatus.INVALID,
                        f"no reading for {minutes:.0f} min",
                        now,
                    )
                )
        return flags
