"""Typed messages between planner, gate, approvers and executor (Pydantic, validated)."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from engine.actions.policy import ActionLevel

Scalar = str | float | int | bool | None


class Evidence(BaseModel, frozen=True):
    """One rule hit the proposal relies on."""

    rule_id: str
    seq: int
    reading_ts: datetime | None
    dq_status: Literal["valid", "suspect", "invalid"]
    sop_ref: str
    observed: dict[str, Scalar] = Field(default_factory=dict)


class Proposal(BaseModel, frozen=True):
    """A planned action. Its id is derived from (incident, action, target), so proposing the
    same thing twice for one incident yields the same id: the gate treats it as a duplicate."""

    incident_id: str
    shipment_id: str
    depot_id: str | None
    action: str
    target: str
    params: dict[str, Scalar] = Field(default_factory=dict)
    rule_ids: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    eta_delay_min: float | None = None
    alternatives: tuple[str, ...] = ()
    rationale: str
    planner: Literal["deterministic", "llm"]
    degraded: bool = False  # an LLM step was asked but gave no valid answer; fallback used
    created_at: datetime

    @property
    def idempotency_key(self) -> str:
        return f"{self.incident_id}:{self.action}:{self.target}"

    @property
    def proposal_id(self) -> str:
        return "PRP-" + self.idempotency_key.replace(":", "-")

    def content_hash(self) -> str:
        """sha256 of the full content. An approval must quote this exact value."""
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Approval(BaseModel, frozen=True):
    approval_id: str = Field(min_length=1)
    proposal_id: str
    proposal_hash: str = Field(min_length=64, max_length=64)
    approver_id: str = Field(min_length=1)
    approver_role: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    reason: str = ""
    decided_at: datetime


class GateOutcome(StrEnum):
    EXECUTE = "execute"  # L2 auto-approved, or L3 approved by a human: hand to the executor
    AWAIT_APPROVAL = "await_approval"  # L3 queued for a human
    REJECTED = "rejected"
    DRY_RUN = "dry_run"  # would have executed; nothing runs while dry_run is on
    DUPLICATE = "duplicate"  # this (incident, action, target) was already decided


class GateDecision(BaseModel, frozen=True):
    proposal_id: str
    proposal_hash: str
    action: str
    level: ActionLevel | None
    outcome: GateOutcome
    reasons: tuple[str, ...] = ()
    policy_version: int
    policy_hash: str
    decided_at: datetime
    approval_id: str | None = None


class ActionStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionRecord(BaseModel, frozen=True):
    action_id: str
    idempotency_key: str
    proposal_id: str
    proposal_hash: str
    incident_id: str
    shipment_id: str
    action: str
    target: str
    level: ActionLevel
    status: ActionStatus
    attempts: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
