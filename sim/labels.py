"""Ground truth produced alongside the telemetry. Never ingested into the legacy table."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from sim.scenario import IncidentType, NoiseType

OracleAction = Literal[
    "no_action",
    "notify_driver_restart_cooling",
    "create_incident_ticket",
    "divert_to_cold_storage",
    "divert_to_overflow_depot",
    "escalate_tier2_manager",
]


class IncidentLabel(BaseModel):
    incident_id: str
    type: IncidentType
    shipment_id: str
    start_ts: datetime  # true start of the incident
    true_breach_ts: datetime | None = None  # first time the true temperature left the band
    oracle_action: OracleAction = "no_action"
    oracle_target: str | None = None  # shipment or facility the oracle action applies to


class GlitchLabel(BaseModel):
    glitch_id: str
    type: NoiseType
    shipment_id: str
    first_seq: int
    last_seq: int
    # Emission ids of the corrupted readings (dropout: none were emitted).
    emit_ids: list[int] = []
    start_ts: datetime | None = None
    end_ts: datetime | None = None


class WorldEvent(BaseModel):
    kind: Literal["port_congestion", "facility_full", "driver_unreachable"]
    subject_id: str  # port, facility or shipment id
    start_ts: datetime
    end_ts: datetime
