"""Typed messages between agents (spec section 6). Validated on publish and on read."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class IncidentOpened(BaseModel, frozen=True):
    type: Literal["IncidentOpened"] = "IncidentOpened"
    incident_id: str
    shipment_id: str
    rule_ids: tuple[str, ...]
    risk_score: float
    triage: str | None = None  # monitor's verdict, if it was asked (C2)
    triage_degraded: bool = False
    replan: bool = False  # True when an open incident's set of rules changed


class PlanProposed(BaseModel, frozen=True):
    type: Literal["PlanProposed"] = "PlanProposed"
    incident_id: str
    proposal_id: str
    proposal_hash: str
    action: str
    target: str
    planner: Literal["deterministic", "llm"]
    close_call: bool
    degraded: bool


class ActionApproved(BaseModel, frozen=True):
    type: Literal["ActionApproved"] = "ActionApproved"
    incident_id: str
    proposal_id: str
    action: str
    level: str
    approved_by: str  # "policy_gate" for L2, "role:person" for L3


class ActionExecuted(BaseModel, frozen=True):
    type: Literal["ActionExecuted"] = "ActionExecuted"
    incident_id: str
    action_id: str
    action: str
    target: str
    attempts: int


class ActionFailed(BaseModel, frozen=True):
    type: Literal["ActionFailed"] = "ActionFailed"
    incident_id: str
    action_id: str
    action: str
    error: str | None
    attempts: int


Message = Annotated[
    IncidentOpened | PlanProposed | ActionApproved | ActionExecuted | ActionFailed,
    Field(discriminator="type"),
]


class Envelope(BaseModel, frozen=True):
    message_id: int
    correlation_id: str
    producer: str
    created_at: datetime
    body: Message


def correlation_id(incident_id: str) -> str:
    return f"COR-{incident_id}"
