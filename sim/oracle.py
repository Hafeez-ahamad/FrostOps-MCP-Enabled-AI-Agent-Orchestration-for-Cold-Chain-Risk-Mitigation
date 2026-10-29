"""Oracle-best action per incident, decided with full (ground-truth) information.

This is a fixed mapping from the true incident type and the true state, not an optimiser
(see docs/decisions.md). It is the answer key the replay evaluation scores against.

| Incident                         | Oracle action (target)                                   |
|----------------------------------|----------------------------------------------------------|
| reefer_failure, ETA delay <= 60  | notify_driver_restart_cooling (shipment)          SOP §1 |
| reefer_failure, ETA delay > 60   | divert_to_cold_storage (nearest site with room)   SOP §1 |
| facility_full (+ reefer failure) | divert_to_cold_storage (nearest site that is not full)   |
| driver_unreachable (+ failure)   | escalate_tier2_manager (nobody can restart the unit)     |
| door_open, true breach           | create_incident_ticket (self-recovers; record it)        |
| door_open, no breach             | no_action                                                |
| route_delay_cascade              | escalate_tier2_manager (shipment)                 SOP §3 |
| port_congestion_spike            | divert_to_overflow_depot (Inland Empire Overflow) SOP §2 |
"""

from datetime import datetime

from sim.labels import IncidentLabel
from sim.world import World


def nearest_available_cold_storage(
    world: World, lat: float, lon: float, full_facility_ids: set[str]
) -> str:
    for site in world.cold_storage_by_distance(lat, lon):
        if site.id not in full_facility_ids:
            return site.id
    raise ValueError("no cold-storage facility has capacity")


def assign_oracle(
    label: IncidentLabel,
    world: World,
    *,
    eta_delay_min: float,
    position: tuple[float, float],
    full_facility_ids: set[str],
) -> IncidentLabel:
    """Fill in oracle_action/oracle_target. `true_breach_ts` must already be set."""
    action: str
    target: str | None = label.shipment_id
    if label.type == "reefer_failure" and eta_delay_min <= 60:
        action = "notify_driver_restart_cooling"
    elif label.type in ("reefer_failure", "facility_full"):
        action = "divert_to_cold_storage"
        target = nearest_available_cold_storage(world, *position, full_facility_ids)
    elif label.type in ("driver_unreachable", "route_delay_cascade"):
        action = "escalate_tier2_manager"
    elif label.type == "door_open":
        action = "create_incident_ticket" if label.true_breach_ts else "no_action"
        target = label.shipment_id if label.true_breach_ts else None
    else:  # port_congestion_spike
        action = "divert_to_overflow_depot"
        target = world.overflow_depot().id
    return label.model_copy(update={"oracle_action": action, "oracle_target": target})


def full_facilities_at(events: list[tuple[str, datetime, datetime]], at: datetime) -> set[str]:
    """Facility ids that are full at `at`, from (facility_id, start, end) windows."""
    return {fid for fid, start, end in events if start <= at < end}
