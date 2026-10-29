"""Phase 1 'done when': the data-quality layer flags the seeded glitches, and only them."""

from engine.dq.stream import StreamResult
from sim.generator import SimulationOutput


def _statuses(smoke: SimulationOutput, smoke_dq: StreamResult) -> dict[int, str]:
    return {
        int(emit_id): a.status.value
        for emit_id, a in zip(smoke.readings["gt_emit_id"], smoke_dq.assessments, strict=True)
    }


def test_every_seeded_glitch_is_flagged(smoke: SimulationOutput, smoke_dq: StreamResult) -> None:
    status = _statuses(smoke, smoke_dq)
    missed = []
    for g in smoke.glitches:
        if g.type == "dropout":
            assert g.start_ts is not None and g.end_ts is not None
            detected = any(
                f.shipment_id == g.shipment_id and g.start_ts <= f.evaluated_at
                for f in smoke_dq.freshness_flags
            )
        else:
            detected = any(status[e] != "valid" for e in g.emit_ids)
        if not detected:
            missed.append((g.glitch_id, g.type))
    assert missed == []


def test_no_clean_reading_is_flagged(smoke: SimulationOutput, smoke_dq: StreamResult) -> None:
    clean = smoke.readings["gt_glitch_id"].isna()
    flagged = [
        a
        for a, ok in zip(smoke_dq.assessments, clean, strict=True)
        if ok and a.status.value != "valid"
    ]
    assert flagged == []


def test_real_incident_readings_are_not_hidden_as_glitches(
    smoke: SimulationOutput, smoke_dq: StreamResult
) -> None:
    """A genuine breach must reach the rules: DQ must not mark it suspect or invalid."""
    in_incident = smoke.readings["gt_incident_id"].notna()
    hidden = [
        a
        for a, inc in zip(smoke_dq.assessments, in_incident, strict=True)
        if inc and a.status.value != "valid"
    ]
    assert hidden == []


def test_freshness_only_fires_for_dropouts(smoke: SimulationOutput, smoke_dq: StreamResult) -> None:
    dropout_shipments = {g.shipment_id for g in smoke.glitches if g.type == "dropout"}
    assert {f.shipment_id for f in smoke_dq.freshness_flags} == dropout_shipments


def test_seeded_glitches_cannot_reach_an_sop_threshold_as_valid(
    smoke: SimulationOutput, smoke_dq: StreamResult
) -> None:
    """The safety point: a glitchy value that would breach the FRESH band is never `valid`."""
    r = smoke.readings
    for a, (_, row) in zip(smoke_dq.assessments, r.iterrows(), strict=True):
        if isinstance(row["gt_glitch_id"], str) and row["PROD_CLS"] == "FRESH":
            if row["RFR_TMP_C"] > 4.0:
                assert a.status.value != "valid", row["gt_glitch_id"]
