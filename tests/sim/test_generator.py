"""Simulator: reproducibility, fleet shape, incident and noise ground truth."""

import hashlib

import pandas as pd
import pytest
from pydantic import ValidationError

from engine.data.schema import RAW_COLUMNS
from sim.generator import SimulationOutput, generate
from sim.incidents import ScenarioError
from sim.scenario import Scenario
from sim.world import load_world

INCIDENT_TYPES = {
    "reefer_failure",
    "door_open",
    "route_delay_cascade",
    "port_congestion_spike",
    "driver_unreachable",
    "facility_full",
}
NOISE_TYPES = {
    "spike",
    "flatline",
    "dropout",
    "duplicate",
    "out_of_order",
    "unit_mixup",
    "gps_jump",
    "clock_skew",
}


def _digest(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest()


def test_same_seed_gives_identical_output(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    again = generate(smoke_scenario)
    assert _digest(again.readings) == _digest(smoke.readings)
    assert again.incidents == smoke.incidents
    assert again.glitches == smoke.glitches


def test_different_seed_gives_different_output(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    other = generate(smoke_scenario.with_seed(smoke_scenario.seed + 1))
    assert _digest(other.readings) != _digest(smoke.readings)


def test_readings_use_the_legacy_columns(smoke: SimulationOutput) -> None:
    assert list(smoke.readings.columns[: len(RAW_COLUMNS)]) == RAW_COLUMNS
    extra = set(smoke.readings.columns) - set(RAW_COLUMNS)
    assert all(c.startswith("gt_") for c in extra)


def test_stream_is_in_receive_order(smoke: SimulationOutput) -> None:
    assert smoke.readings["RCPT_DTTM"].is_monotonic_increasing


def test_fleet_is_in_southern_california_with_configured_cargo(smoke: SimulationOutput) -> None:
    world = load_world()
    r = smoke.readings[smoke.readings["gt_glitch_id"].isna()]
    assert r["GPS_Y"].between(32.0, 35.5).all()
    assert r["GPS_X"].between(-121.0, -114.0).all()
    assert set(r["PROD_CLS"]) == set(world.cargo_types)
    assert set(r["HUB_CD"]) <= {d.id for d in world.depots}


def test_scenario_rejects_fleet_size_outside_spec(smoke_scenario: Scenario) -> None:
    with pytest.raises(ValidationError):
        Scenario(**{**smoke_scenario.model_dump(), "vehicles": 49})
    with pytest.raises(ValidationError):
        Scenario(**{**smoke_scenario.model_dump(), "vehicles": 201})


def test_impossible_scenario_fails_loudly(smoke_scenario: Scenario) -> None:
    greedy = smoke_scenario.model_copy(
        update={"incidents": [smoke_scenario.incidents[0].model_copy(update={"count": 500})]}
    )
    with pytest.raises(ScenarioError):
        generate(greedy)


# --- clean data never trips an SOP threshold on its own ------------------------------------


def test_clean_readings_never_cross_sop_thresholds(smoke: SimulationOutput) -> None:
    r = smoke.readings
    clean = r[r["gt_incident_id"].isna() & r["gt_glitch_id"].isna()]
    world = load_world()
    for code, band in world.cargo_types.items():
        temps = clean.loc[clean["PROD_CLS"] == code, "RFR_TMP_C"]
        assert temps.between(band.min_c, band.max_c).all(), code
    assert not (clean["PORT_Q_IX"] > 7.0).any()
    assert not ((clean["HZD_TIER"] == "High Risk") & (clean["LATE_PCT"] > 0.65)).any()


# --- incidents --------------------------------------------------------------------------


def test_every_incident_type_is_seeded_and_labelled(smoke: SimulationOutput) -> None:
    assert {lb.type for lb in smoke.incidents} == INCIDENT_TYPES
    for lb in smoke.incidents:
        assert lb.oracle_action in {
            "no_action",
            "notify_driver_restart_cooling",
            "create_incident_ticket",
            "divert_to_cold_storage",
            "divert_to_overflow_depot",
            "escalate_tier2_manager",
        }


def _incident_rows(smoke: SimulationOutput, incident_id: str) -> pd.DataFrame:
    r = smoke.readings
    return r[(r["gt_incident_id"] == incident_id) & r["gt_glitch_id"].isna()]


def test_reefer_failures_breach_the_band(smoke: SimulationOutput) -> None:
    failures = [
        lb
        for lb in smoke.incidents
        if lb.type in ("reefer_failure", "driver_unreachable", "facility_full")
    ]
    assert failures
    for lb in failures:
        assert lb.true_breach_ts is not None and lb.true_breach_ts > lb.start_ts, lb.incident_id


def test_reefer_failure_oracle_follows_eta_rule(smoke: SimulationOutput) -> None:
    for lb in smoke.incidents:
        if lb.type != "reefer_failure":
            continue
        rows = _incident_rows(smoke, lb.incident_id)
        long_delay = rows["ARR_SLIP_MIN"].max() > 60
        expected = "divert_to_cold_storage" if long_delay else "notify_driver_restart_cooling"
        assert lb.oracle_action == expected, lb.incident_id


def test_facility_full_oracle_avoids_the_full_site(smoke: SimulationOutput) -> None:
    full = {e.subject_id for e in smoke.events if e.kind == "facility_full"}
    for lb in smoke.incidents:
        if lb.type == "facility_full":
            assert lb.oracle_action == "divert_to_cold_storage"
            assert lb.oracle_target not in full


def test_port_spike_pushes_congestion_above_seven(smoke: SimulationOutput) -> None:
    spiked = [lb for lb in smoke.incidents if lb.type == "port_congestion_spike"]
    assert spiked
    for lb in spiked:
        assert lb.oracle_target == "IE-OVERFLOW"
        assert (_incident_rows(smoke, lb.incident_id)["PORT_Q_IX"] > 7.0).any()


def test_delay_cascade_meets_the_tier2_escalation_condition(smoke: SimulationOutput) -> None:
    for lb in smoke.incidents:
        if lb.type == "route_delay_cascade":
            rows = _incident_rows(smoke, lb.incident_id)
            hit = (rows["HZD_TIER"] == "High Risk") & (rows["LATE_PCT"] > 0.65)
            assert hit.any(), lb.incident_id
            assert lb.oracle_action == "escalate_tier2_manager"


def test_driver_unreachable_has_a_world_event(smoke: SimulationOutput) -> None:
    unreachable = {e.subject_id for e in smoke.events if e.kind == "driver_unreachable"}
    for lb in smoke.incidents:
        if lb.type == "driver_unreachable":
            assert lb.shipment_id in unreachable


# --- noise ------------------------------------------------------------------------------


def test_every_noise_type_is_seeded_on_incident_free_shipments(smoke: SimulationOutput) -> None:
    assert {g.type for g in smoke.glitches} == NOISE_TYPES
    incident_shipments = {lb.shipment_id for lb in smoke.incidents}
    assert not incident_shipments & {g.shipment_id for g in smoke.glitches}


def test_glitches_change_the_stream_as_documented(smoke: SimulationOutput) -> None:
    r = smoke.readings.set_index("gt_emit_id")
    for g in smoke.glitches:
        rows = r.loc[g.emit_ids]
        truth = rows["gt_true_temp_c"]
        if g.type == "dropout":
            assert g.emit_ids == []
            sent = smoke.readings[smoke.readings["CNSG_NO"] == g.shipment_id]["PKT_SEQ"]
            assert not sent.between(g.first_seq, g.last_seq).any()
        elif g.type == "spike":
            assert (rows["RFR_TMP_C"] - truth).min() >= 9.5
        elif g.type == "flatline":
            assert rows["RFR_TMP_C"].nunique() == 1 and len(rows) >= 8
        elif g.type == "unit_mixup":
            assert ((rows["RFR_TMP_C"] - (truth * 1.8 + 32)).abs() < 0.5).all()
        elif g.type == "duplicate":
            copy = rows.iloc[0]
            same = smoke.readings[
                (smoke.readings["CNSG_NO"] == copy["CNSG_NO"])
                & (smoke.readings["PKT_SEQ"] == copy["PKT_SEQ"])
            ]
            assert len(same) == 2
        elif g.type == "out_of_order":
            assert (rows["RCPT_DTTM"] > rows["EVT_DTTM"]).all()
        elif g.type == "clock_skew":
            assert (rows["EVT_DTTM"] > rows["RCPT_DTTM"]).all()
        elif g.type == "gps_jump":
            assert len(rows) == 1


def test_ground_truth_covers_delayed_shipments_drifting_into_a_port_spike() -> None:
    """Regression: a port-bound truck planned to arrive before a spike, but delayed into it,
    crossed 7.0 without a label, so the detector's correct alert counted as a false alarm.
    Seed 1 of detection_eval reproduced it."""
    from engine.settings import PROJECT_ROOT
    from sim.scenario import load_scenario

    scenario = load_scenario(PROJECT_ROOT / "sim" / "scenarios" / "detection_eval.yaml")
    r = generate(scenario.with_seed(1)).readings
    unlabelled_spiked = r[(r["PORT_Q_IX"] > 7.0) & r["gt_incident_id"].isna()]
    assert unlabelled_spiked.empty
