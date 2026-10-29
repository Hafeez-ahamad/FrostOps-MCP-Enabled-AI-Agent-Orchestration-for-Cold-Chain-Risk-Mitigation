"""Dispatch: incident events -> the configuration's decision graph -> gate -> executor.

- An opened incident, or a merged one whose set of rules changed, runs the decision graph
  (C0, C1 or C2; see engine/graphs). The graph plans, lets an LLM weigh in if the
  configuration says so, and submits to the gate, falling back to the next candidate only
  when a precondition failed.
- Executor outcomes are published as ActionExecuted / ActionFailed messages.
- For a temperature incident the ambient weather at the vehicle is read through a StaleCache.
  A failed read uses the last good value, marked stale; either way the plan gets a note and low
  confidence. Weather is context only: no L2 action and no precondition depends on it.
- When an action fails after its retries, the incident is marked needs_attention and a Tier 2
  escalation is proposed. This path is deterministic in every configuration; escalation is
  L3, so a human still has to approve it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.actions.models import ActionRecord, ActionStatus, GateDecision
from engine.agents.context import ReadingPoint
from engine.bus.messages import ActionExecuted, ActionFailed, correlation_id
from engine.graphs.nodes import DecisionContext, DecisionState, submit_candidate
from engine.incidents.manager import IncidentEvent, IncidentManager
from engine.integrations.clients import FacilityList, FacilityStatus, Integrations, StaleCache
from engine.planner.planner import Candidate, Plan
from engine.rules.engine import RuleInput

ESCALATION = "escalate_tier2_manager"


@dataclass
class Dispatcher:
    ctx: DecisionContext
    graph: Any  # compiled LangGraph decision graph
    integrations: Integrations
    manager: IncidentManager
    decisions: list[GateDecision] = field(default_factory=list)
    _planned_rules: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _last_reading: dict[str, RuleInput] = field(default_factory=dict)
    weather: StaleCache = field(default_factory=StaleCache)

    def _facilities(self) -> list[FacilityStatus] | None:
        result = self.integrations.facilities.list()
        return FacilityList.model_validate(result.data).facilities if result.ok else None

    def _weather_note(self, incident_rules: tuple[str, ...], reading: RuleInput) -> str | None:
        if "R-TEMP-01" not in incident_rules:
            return None
        if reading.latitude is None or reading.longitude is None:
            return None
        key = f"{reading.latitude:.1f},{reading.longitude:.1f}"
        value, stale = self.weather.read(
            key, self.integrations.weather.get(reading.latitude, reading.longitude)
        )
        if value is None:
            return "weather unavailable: ambient conditions unknown"
        if stale:
            return f"weather stale: last good reading from {value['observed_at']}"
        return None

    def on_event(
        self,
        event: IncidentEvent,
        reading: RuleInput,
        now: datetime,
        window: Sequence[ReadingPoint] = (),
    ) -> list[GateDecision]:
        incident = event.incident
        if event.kind in ("cleared", "needs_attention"):
            return []
        self._last_reading[incident.incident_id] = reading
        if self._planned_rules.get(incident.incident_id) == incident.rule_ids:
            return []
        replan = incident.incident_id in self._planned_rules
        self._planned_rules[incident.incident_id] = incident.rule_ids

        state: DecisionState = {
            "replan": replan,
            "incident": incident,
            "reading": reading,
            "window": list(window),
            "facilities": self._facilities(),
            "now": now,
        }
        if note := self._weather_note(incident.rule_ids, reading):
            state["weather_note"] = note
        result = self.graph.invoke(state)
        made: list[GateDecision] = result.get("decisions", [])
        self.decisions.extend(made)
        return made

    def on_actions(self, records: list[ActionRecord], now: datetime) -> list[GateDecision]:
        """Publish executor outcomes; escalate actions that failed for good."""
        made: list[GateDecision] = []
        for record in records:
            cor = correlation_id(record.incident_id)
            if record.status is ActionStatus.SUCCEEDED:
                self.ctx.bus.publish(
                    cor,
                    "executor",
                    ActionExecuted(
                        incident_id=record.incident_id,
                        action_id=record.action_id,
                        action=record.action,
                        target=record.target,
                        attempts=record.attempts,
                    ),
                    now,
                )
                continue
            if record.status is not ActionStatus.FAILED:
                continue
            self.ctx.bus.publish(
                cor,
                "executor",
                ActionFailed(
                    incident_id=record.incident_id,
                    action_id=record.action_id,
                    action=record.action,
                    error=record.last_error,
                    attempts=record.attempts,
                ),
                now,
            )
            if record.action == ESCALATION or (record.last_error or "").startswith("refused"):
                continue  # refusals are audited; a failed escalation is already human-facing
            self.manager.mark_needs_attention(record.incident_id, now)
            incident = self.manager.incidents.get(record.incident_id)
            reading = self._last_reading.get(record.incident_id)
            if incident is None or reading is None:
                continue
            candidate = Candidate(
                ESCALATION,
                incident.shipment_id,
                50.0,
                0.0,
                5.0,
                f"{record.action} failed after retries ({record.last_error}): needs a human",
            )
            state: DecisionState = {
                "incident": incident,
                "reading": reading,
                "now": now,
                "plan": Plan(incident.incident_id, (candidate,)),
            }
            decision = submit_candidate(self.ctx, state, candidate, "deterministic", False)
            made.append(decision)
            self.decisions.append(decision)
        return made
