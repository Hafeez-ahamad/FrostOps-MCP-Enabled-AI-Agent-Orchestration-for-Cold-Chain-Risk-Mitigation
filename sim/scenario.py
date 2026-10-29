"""Scenario files (sim/scenarios/*.yaml): seed, fleet size, incident and noise schedule."""

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

IncidentType = Literal[
    "reefer_failure",
    "door_open",
    "route_delay_cascade",
    "port_congestion_spike",
    "driver_unreachable",
    "facility_full",
]

NoiseType = Literal[
    "spike",
    "flatline",
    "dropout",
    "duplicate",
    "out_of_order",
    "unit_mixup",
    "gps_jump",
    "clock_skew",
]


class IncidentSpec(BaseModel, frozen=True):
    type: IncidentType
    at_hour: float = Field(ge=0)
    count: int = Field(default=1, ge=1)
    # reefer_failure: ETA delay that develops alongside the failure (R-ETA-01 needs > 60).
    eta_delay_min: float = Field(default=0.0, ge=0, le=180)
    # port_congestion_spike / facility_full: how long the condition lasts.
    duration_hours: float = Field(default=4.0, gt=0)
    port: str | None = None  # port_congestion_spike
    facility: str | None = None  # facility_full

    @model_validator(mode="after")
    def _required_fields(self) -> "IncidentSpec":
        if self.type == "port_congestion_spike" and not self.port:
            raise ValueError("port_congestion_spike needs `port`")
        if self.type == "facility_full" and not self.facility:
            raise ValueError("facility_full needs `facility`")
        return self


class NoiseSpec(BaseModel, frozen=True):
    type: NoiseType
    count: int = Field(ge=1)


class Scenario(BaseModel, frozen=True):
    name: str
    seed: int = Field(ge=0)
    start: datetime
    duration_hours: float = Field(gt=0, le=24 * 60)
    reading_interval_min: int = Field(default=5, ge=1, le=15)
    vehicles: int = Field(ge=50, le=200)  # spec section 7.1: 50-200 vehicles
    port_bound_share: float = Field(default=0.3, ge=0, le=1)
    incidents: list[IncidentSpec] = Field(default_factory=list)
    noise: list[NoiseSpec] = Field(default_factory=list)

    def with_seed(self, seed: int) -> "Scenario":
        return self.model_copy(update={"seed": seed})


def load_scenario(path: Path) -> Scenario:
    return Scenario(**yaml.safe_load(path.read_text(encoding="utf-8")))
