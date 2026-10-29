"""Stepwise fleet simulator on a virtual clock. Same scenario and seed -> identical output.

Each tick (default 5 simulated minutes) it updates port congestion, starts departing trips,
advances every active shipment's physics and delay, and emits one reading per shipment in the
legacy column format, with any planned noise applied. Ground truth travels alongside in `gt_*`
columns, which are never ingested.

Separate random streams (spawned from the seed) drive the schedule, incidents, noise and
physics, so adding a glitch never changes the underlying fleet.
"""

import json
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from engine.cargo import CargoType
from sim.incidents import IncidentPlan, ShipmentEffects, plan_incidents
from sim.labels import GlitchLabel, IncidentLabel, WorldEvent
from sim.noise import GlitchPlan, plan_noise
from sim.oracle import assign_oracle, full_facilities_at
from sim.physics import ambient_c, step_temperature
from sim.scenario import Scenario
from sim.schedule import MAX_DELAY_MIN, PlannedShipment, plan_shipments
from sim.world import World, load_world

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
SENSOR_NOISE_SD_C = 0.08
DAY_OFFSET_SD_C = 1.5
CONGESTION_BASE = 3.5
CONGESTION_NATURAL_MAX = 6.8  # without a seeded spike, congestion never reaches the SOP's 7.0
CONGESTION_REVERSION = 0.02
CONGESTION_STEP_SD = 0.08
CONGESTION_SLOWDOWN = 0.5  # extra delay minutes per minute while the destination port is > 7.0


def delay_probability(delay_min: float, port_congestion: float | None) -> float:
    """Logistic in ETA delay (and congestion for port-bound trips). Documented assumption."""
    z = -2.5 + delay_min / 25.0
    if port_congestion is not None:
        z += 0.4 * max(0.0, port_congestion - 5.0)
    return 1.0 / (1.0 + math.exp(-z))


def risk_class(route_risk: float, delay_prob: float) -> str:
    if route_risk >= 7.0 or delay_prob >= 0.6:
        return "High Risk"
    if route_risk >= 4.0 or delay_prob >= 0.3:
        return "Moderate Risk"
    return "Low Risk"


def _overlap_min(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 60)


@dataclass
class _Active:
    plan: PlannedShipment
    band: CargoType
    effects: ShipmentEffects
    temp_c: float
    last_tick: datetime
    delay_min: float = 0.0
    degree_min: float = 0.0
    seq: int = 0
    flat_value: float | None = None


@dataclass
class SimulationOutput:
    scenario: Scenario
    readings: pd.DataFrame  # legacy columns + gt_* columns, in receive order
    shipments: pd.DataFrame
    incidents: list[IncidentLabel]
    glitches: list[GlitchLabel]
    events: list[WorldEvent]
    true_temps: dict[str, list[tuple[datetime, float]]] = field(repr=False, default_factory=dict)
    # What sim/counterfactual.py needs to re-step a truck's physics.
    plans: dict[str, PlannedShipment] = field(repr=False, default_factory=dict)
    day_offsets: dict[date, float] = field(repr=False, default_factory=dict)
    cargo_types: dict[str, CargoType] = field(repr=False, default_factory=dict)

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.readings.to_csv(out_dir / "readings.csv", index=False)
        self.shipments.to_csv(out_dir / "shipments.csv", index=False)
        for name, items in (
            ("incidents", self.incidents),
            ("glitches", self.glitches),
            ("events", self.events),
        ):
            data = [item.model_dump(mode="json") for item in items]
            (out_dir / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
        (out_dir / "scenario.json").write_text(
            self.scenario.model_dump_json(indent=1), encoding="utf-8"
        )


class _Simulator:
    def __init__(self, scenario: Scenario, world: World) -> None:
        self.scenario = scenario
        self.world = world
        streams = np.random.SeedSequence(scenario.seed).spawn(4)
        rng_plan, rng_incident, rng_noise, self.rng = (np.random.default_rng(s) for s in streams)
        self.plans = plan_shipments(scenario, world, rng_plan)
        self.incidents: IncidentPlan = plan_incidents(scenario, world, self.plans, rng_incident)
        self.glitch_plans = plan_noise(scenario, self.plans, self.incidents.reserved, rng_noise)
        self.glitch_by_shipment = {g.shipment_id: g for g in self.glitch_plans}
        self.incident_by_shipment = {lb.shipment_id: lb.incident_id for lb in self.incidents.labels}
        self.congestion = {port.id: CONGESTION_BASE for port in world.ports}
        self.day_offsets: dict[date, float] = {}
        self.readings: list[dict[str, Any]] = []
        self.true_temps: dict[str, list[tuple[datetime, float]]] = {}
        self.glitch_emits: dict[str, list[int]] = {g.glitch_id: [] for g in self.glitch_plans}
        self.glitch_times: dict[str, list[datetime]] = {g.glitch_id: [] for g in self.glitch_plans}

    # --- world state -------------------------------------------------------------------
    def _day_offset(self, day: date) -> float:
        if day not in self.day_offsets:
            self.day_offsets[day] = float(self.rng.normal(0.0, DAY_OFFSET_SD_C))
        return self.day_offsets[day]

    def _update_congestion(self, t: datetime) -> None:
        for port_id in sorted(self.congestion):
            spike = next(
                (
                    lvl
                    for pid, s, e, lvl in self.incidents.port_spikes
                    if pid == port_id and s <= t < e
                ),
                None,
            )
            step = float(self.rng.normal(0.0, CONGESTION_STEP_SD))
            if spike is not None:
                self.congestion[port_id] = min(10.0, max(7.05, spike + step))
            else:
                level = self.congestion[port_id]
                level += CONGESTION_REVERSION * (CONGESTION_BASE - level) + step
                self.congestion[port_id] = min(CONGESTION_NATURAL_MAX, max(0.0, level))

    # --- one shipment ------------------------------------------------------------------
    def _advance(self, a: _Active, t: datetime) -> None:
        minutes = (t - a.last_tick).total_seconds() / 60
        if minutes <= 0:
            return
        p, eff = a.plan, a.effects
        reefer_on = eff.reefer_off_at is None or eff.reefer_off_at >= t
        door_min = 0.0
        if eff.door_open is not None:
            start, dur = eff.door_open
            door_min = _overlap_min(a.last_tick, t, start, start + timedelta(minutes=dur))
        ambient = ambient_c(t, p.depot.inland, self._day_offset(t.date()))
        a.temp_c = step_temperature(
            a.temp_c, ambient, a.band.setpoint_c, reefer_on, door_min, minutes
        )
        if a.temp_c > a.band.max_c:
            a.degree_min += (a.temp_c - a.band.max_c) * minutes

        delay = a.delay_min + p.base_delay_min / p.planned_min * minutes
        for start, ramp, total in eff.delay_ramps:
            delay += (
                total * _overlap_min(a.last_tick, t, start, start + timedelta(minutes=ramp)) / ramp
            )
        if p.port_id and self.congestion[p.port_id] > 7.0:
            delay += CONGESTION_SLOWDOWN * minutes
        a.delay_min = min(MAX_DELAY_MIN, delay)

    def _emit(self, a: _Active, t: datetime, progress: float, arrived: bool) -> None:
        p = a.plan
        a.seq += 1
        self.true_temps.setdefault(p.shipment_id, []).append((t, a.temp_c))
        # Always draw the same random numbers, whatever the noise plan, so noise never shifts
        # the rest of the stream.
        measured = round(a.temp_c + float(self.rng.normal(0.0, SENSOR_NOISE_SD_C)), 2)
        latency_s = int(self.rng.integers(5, 61))

        lat, lon = p.position_at_progress(progress)
        congestion = self.congestion[p.port_id] if p.port_id else None
        route_risk = min(10.0, p.route_risk + sum(b for s, b in a.effects.risk_bumps if s < t))
        prob = delay_probability(a.delay_min, congestion)
        ts, rcv = t, t + timedelta(seconds=latency_s)

        glitch = self.glitch_by_shipment.get(p.shipment_id)
        active = glitch if glitch is not None and glitch.covers(a.seq) else None
        if active is not None:
            self.glitch_times[active.glitch_id].append(t)
            if active.type == "dropout":
                return
            measured, lat, lon, ts, rcv = self._corrupt(active, a, measured, lat, lon, ts, rcv)

        row: dict[str, Any] = {
            "EVT_DTTM": ts.strftime(TS_FORMAT),
            "RCPT_DTTM": rcv.strftime(TS_FORMAT),
            "CNSG_NO": p.shipment_id,
            "TRLR_NO": p.vehicle_id,
            "HUB_CD": p.depot.id,
            "PROD_CLS": p.cargo_type,
            "PKT_SEQ": a.seq,
            "GPS_Y": round(lat, 5),
            "GPS_X": round(lon, 5),
            "RFR_TMP_C": measured,
            "LOAD_QLTY_IX": round(max(0.0, 1.0 - a.degree_min / 600.0), 3),
            "HZD_TIER": risk_class(route_risk, prob),
            "LATE_PCT": round(prob, 3),
            "PORT_Q_IX": None if congestion is None else round(congestion, 2),
            "LANE_HZD_IX": round(route_risk, 2),
            "ARR_SLIP_MIN": round(a.delay_min, 1),
            "LEG_STAT": "A" if arrived else "E",
            "LOAD_FLG": "Y",
            "gt_emit_id": len(self.readings),
            "gt_true_temp_c": round(a.temp_c, 3),
            "gt_incident_id": self.incident_by_shipment.get(p.shipment_id),
            "gt_glitch_id": None,
        }
        if active is None:
            self.readings.append(row)
            return
        if active.type == "duplicate":
            self.readings.append(row)
            row = {
                **row,
                "gt_emit_id": len(self.readings),
                "RCPT_DTTM": (rcv + timedelta(seconds=1)).strftime(TS_FORMAT),
            }
        row["gt_glitch_id"] = active.glitch_id
        self.glitch_emits[active.glitch_id].append(row["gt_emit_id"])
        self.readings.append(row)

    @staticmethod
    def _corrupt(
        g: GlitchPlan,
        a: _Active,
        measured: float,
        lat: float,
        lon: float,
        ts: datetime,
        rcv: datetime,
    ) -> tuple[float, float, float, datetime, datetime]:
        if g.type == "spike":
            measured = round(measured + g.magnitude, 2)
        elif g.type == "flatline":
            if a.seq == g.first_seq or a.flat_value is None:
                a.flat_value = measured
            measured = a.flat_value
        elif g.type == "unit_mixup":
            measured = round(measured * 1.8 + 32.0, 2)
        elif g.type == "gps_jump":
            lat, lon = lat + g.magnitude, lon + g.lon_offset
        elif g.type == "clock_skew":
            ts = ts + timedelta(minutes=g.magnitude)
        elif g.type == "out_of_order":
            rcv = rcv + timedelta(minutes=g.magnitude)
        return measured, lat, lon, ts, rcv

    # --- main loop ---------------------------------------------------------------------
    def run(self) -> SimulationOutput:
        s = self.scenario
        interval = timedelta(minutes=s.reading_interval_min)
        end = s.start + timedelta(hours=s.duration_hours)
        pending = deque(self.plans)
        active: dict[str, _Active] = {}
        t = s.start
        while t <= end:
            self._update_congestion(t)
            while pending and pending[0].depart <= t:
                p = pending.popleft()
                band = self.world.cargo_types[p.cargo_type]
                active[p.shipment_id] = _Active(
                    plan=p,
                    band=band,
                    effects=self.incidents.effects.get(p.shipment_id, ShipmentEffects()),
                    temp_c=band.setpoint_c + float(self.rng.normal(0.0, 0.3)),
                    last_tick=t,
                )
            for shipment_id in list(active):
                a = active[shipment_id]
                self._advance(a, t)
                a.last_tick = t
                elapsed = (t - a.plan.depart).total_seconds() / 60 - a.delay_min
                progress = elapsed / a.plan.planned_min
                arrived = progress >= 1.0
                self._emit(a, t, progress, arrived)
                if arrived:
                    del active[shipment_id]
            t += interval
        return self._output()

    def _label_port_exposure(self) -> list[IncidentLabel]:
        """Label port-spike incidents from what actually happened.

        A shipment is affected if it was on the road while its port was spiked, which
        depends on its real (delayed) timeline, so this can only be decided after the run.
        """
        labels = []
        next_id = len(self.incidents.labels) + 1
        for shipment_id, _port, t0, t1 in self.incidents.port_exposure:
            ticks = [t for t, _ in self.true_temps.get(shipment_id, []) if t0 <= t < t1]
            if not ticks:
                continue
            labels.append(
                IncidentLabel(
                    incident_id=f"INC-{next_id:03d}",
                    type="port_congestion_spike",
                    shipment_id=shipment_id,
                    start_ts=ticks[0],
                )
            )
            next_id += 1
        return labels

    def _output(self) -> SimulationOutput:
        port_labels = self._label_port_exposure()
        for label in port_labels:
            for row in self.readings:
                if row["CNSG_NO"] == label.shipment_id:
                    row["gt_incident_id"] = label.incident_id
        frame = pd.DataFrame(self.readings)
        frame = frame.sort_values(["RCPT_DTTM", "CNSG_NO", "PKT_SEQ", "gt_emit_id"], kind="stable")
        frame = frame.reset_index(drop=True)
        return SimulationOutput(
            scenario=self.scenario,
            readings=frame,
            shipments=pd.DataFrame(
                {
                    "shipment_id": p.shipment_id,
                    "vehicle_id": p.vehicle_id,
                    "depot_id": p.depot.id,
                    "cargo_type": p.cargo_type,
                    "destination_id": p.destination.id,
                    "port_id": p.port_id,
                    "depart": p.depart.strftime(TS_FORMAT),
                    "planned_min": round(p.planned_min, 1),
                }
                for p in self.plans
            ),
            incidents=[self._finish_label(lb) for lb in [*self.incidents.labels, *port_labels]],
            glitches=[self._glitch_label(g) for g in self.glitch_plans],
            events=self.incidents.events,
            true_temps=self.true_temps,
            plans={p.shipment_id: p for p in self.plans},
            day_offsets=dict(self.day_offsets),
            cargo_types=dict(self.world.cargo_types),
        )

    def _finish_label(self, label: IncidentLabel) -> IncidentLabel:
        plan = next(p for p in self.plans if p.shipment_id == label.shipment_id)
        band = self.world.cargo_types[plan.cargo_type]
        breach = next(
            (
                t
                for t, temp in self.true_temps.get(label.shipment_id, [])
                if t >= label.start_ts and not band.min_c <= temp <= band.max_c
            ),
            None,
        )
        label = label.model_copy(update={"true_breach_ts": breach})
        return assign_oracle(
            label,
            self.world,
            eta_delay_min=self.incidents.eta_delay_min.get(label.incident_id, 0.0),
            position=plan.planned_position(label.start_ts),
            full_facility_ids=full_facilities_at(self.incidents.facility_full, label.start_ts),
        )

    def _glitch_label(self, g: GlitchPlan) -> GlitchLabel:
        times = self.glitch_times[g.glitch_id]
        return GlitchLabel(
            glitch_id=g.glitch_id,
            type=g.type,
            shipment_id=g.shipment_id,
            first_seq=g.first_seq,
            last_seq=g.last_seq,
            emit_ids=self.glitch_emits[g.glitch_id],
            start_ts=times[0] if times else None,
            end_ts=times[-1] if times else None,
        )


def generate(scenario: Scenario, world: World | None = None) -> SimulationOutput:
    return _Simulator(scenario, world or load_world()).run()
