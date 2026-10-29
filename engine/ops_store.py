"""Persistence for proposals, approvals, actions and control flags (parameterised SQL only).

One implementation for both SQLite (offline tests) and SQL Server.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Engine, func, insert, select, text, update

from engine.actions.models import ActionRecord, ActionStatus, Approval, Proposal
from engine.actions.policy import ActionLevel
from engine.data.schema import ACTIONS, APPROVALS, CONTROL_FLAGS, PROPOSALS

ProposalStatus = Literal["draft", "awaiting_approval", "approved", "rejected", "dry_run"]
ControlName = Literal["kill_switch", "dry_run"]


class ProposalStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(PROPOSALS).where(PROPOSALS.c.ProposalID == proposal_id))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def proposal(self, proposal_id: str) -> Proposal | None:
        row = self.get(proposal_id)
        return Proposal.model_validate_json(row["Payload"]) if row else None

    def insert(self, p: Proposal, level: ActionLevel, status: ProposalStatus, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(PROPOSALS),
                {
                    "ProposalID": p.proposal_id,
                    "ProposalHash": p.content_hash(),
                    "IncidentID": p.incident_id,
                    "ShipmentID": p.shipment_id,
                    "DepotID": p.depot_id,
                    "Action": p.action,
                    "Target": p.target,
                    "Level": level.value,
                    "Status": status,
                    "Payload": p.model_dump_json(),
                    "CreatedAt": at,
                    "UpdatedAt": at,
                },
            )

    def insert_draft(self, p: Proposal, level: ActionLevel, at: datetime) -> None:
        """Store a draft. On SQL Server this goes through ops.usp_InsertDraftProposal, the only
        write the planner login has, which always sets Status = 'draft' (db/roles.sql)."""
        if self.engine.dialect.name == "mssql":
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "EXEC ops.usp_InsertDraftProposal :id, :hash, :incident, :shipment, "
                        ":depot, :action, :target, :level, :payload, :at"
                    ),
                    {
                        "id": p.proposal_id,
                        "hash": p.content_hash(),
                        "incident": p.incident_id,
                        "shipment": p.shipment_id,
                        "depot": p.depot_id,
                        "action": p.action,
                        "target": p.target,
                        "level": level.value,
                        "payload": p.model_dump_json(),
                        "at": at,
                    },
                )
            return
        self.insert(p, level, "draft", at)

    def set_status(self, proposal_id: str, status: ProposalStatus, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(PROPOSALS)
                .where(PROPOSALS.c.ProposalID == proposal_id)
                .values(Status=status, UpdatedAt=at)
            )

    def pending(self) -> list[dict[str, Any]]:
        """The approval queue."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(PROPOSALS)
                .where(PROPOSALS.c.Status == "awaiting_approval")
                .order_by(PROPOSALS.c.CreatedAt, PROPOSALS.c.ProposalID)
            ).mappings()
            return [dict(r) for r in rows]


class ApprovalStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def exists(self, approval_id: str) -> bool:
        with self.engine.connect() as conn:
            found = conn.execute(
                select(APPROVALS.c.ApprovalID).where(APPROVALS.c.ApprovalID == approval_id)
            ).first()
            return found is not None

    def for_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(APPROVALS).where(APPROVALS.c.ProposalID == proposal_id))
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def insert(self, a: Approval, recorded_at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(APPROVALS),
                {
                    "ApprovalID": a.approval_id,
                    "ProposalID": a.proposal_id,
                    "ProposalHash": a.proposal_hash,
                    "ApproverID": a.approver_id,
                    "ApproverRole": a.approver_role,
                    "Decision": a.decision,
                    "Reason": a.reason[:400],
                    "DecidedAt": a.decided_at,
                    "RecordedAt": recorded_at,
                },
            )


def _record(row: Any) -> ActionRecord:
    return ActionRecord(
        action_id=row["ActionID"],
        idempotency_key=row["IdempotencyKey"],
        proposal_id=row["ProposalID"],
        proposal_hash=row["ProposalHash"],
        incident_id=row["IncidentID"],
        shipment_id=row["ShipmentID"],
        action=row["Action"],
        target=row["Target"],
        level=ActionLevel(row["Level"]),
        status=ActionStatus(row["Status"]),
        attempts=row["Attempts"],
        next_attempt_at=row["NextAttemptAt"],
        last_error=row["LastError"],
        result=json.loads(row["Result"]),
        created_at=row["CreatedAt"],
        updated_at=row["UpdatedAt"],
    )


class ActionStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def by_key(self, idempotency_key: str) -> ActionRecord | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(ACTIONS).where(ACTIONS.c.IdempotencyKey == idempotency_key))
                .mappings()
                .first()
            )
            return _record(row) if row else None

    def get(self, action_id: str) -> ActionRecord | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(ACTIONS).where(ACTIONS.c.ActionID == action_id))
                .mappings()
                .first()
            )
            return _record(row) if row else None

    def all(self) -> list[ActionRecord]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(ACTIONS).order_by(ACTIONS.c.CreatedAt, ACTIONS.c.ActionID))
            return [_record(r) for r in rows.mappings()]

    def insert(self, r: ActionRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(ACTIONS),
                {
                    "ActionID": r.action_id,
                    "IdempotencyKey": r.idempotency_key,
                    "ProposalID": r.proposal_id,
                    "ProposalHash": r.proposal_hash,
                    "IncidentID": r.incident_id,
                    "ShipmentID": r.shipment_id,
                    "Action": r.action,
                    "Target": r.target,
                    "Level": r.level.value,
                    "Status": r.status.value,
                    "Attempts": r.attempts,
                    "NextAttemptAt": r.next_attempt_at,
                    "LastError": r.last_error,
                    "Result": json.dumps(r.result, sort_keys=True, default=str),
                    "CreatedAt": r.created_at,
                    "UpdatedAt": r.updated_at,
                },
            )

    def save(self, r: ActionRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(ACTIONS)
                .where(ACTIONS.c.ActionID == r.action_id)
                .values(
                    Status=r.status.value,
                    Attempts=r.attempts,
                    NextAttemptAt=r.next_attempt_at,
                    LastError=r.last_error[:400] if r.last_error else None,
                    Result=json.dumps(r.result, sort_keys=True, default=str),
                    UpdatedAt=r.updated_at,
                )
            )

    def due(self, now: datetime) -> list[ActionRecord]:
        """Queued actions whose next attempt time has come, oldest first."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(ACTIONS)
                .where(ACTIONS.c.Status == ActionStatus.QUEUED.value)
                .where((ACTIONS.c.NextAttemptAt.is_(None)) | (ACTIONS.c.NextAttemptAt <= now))
                .order_by(ACTIONS.c.CreatedAt, ACTIONS.c.ActionID)
            )
            return [_record(r) for r in rows.mappings()]

    def count_since(
        self,
        since: datetime,
        *,
        level: ActionLevel | None = None,
        action: str | None = None,
        shipment_id: str | None = None,
    ) -> int:
        """Actions created since `since` (any status except cancelled), for rate limits."""
        query = select(func.count()).select_from(ACTIONS).where(ACTIONS.c.CreatedAt > since)
        query = query.where(ACTIONS.c.Status != ActionStatus.CANCELLED.value)
        if level is not None:
            query = query.where(ACTIONS.c.Level == level.value)
        if action is not None:
            query = query.where(ACTIONS.c.Action == action)
        if shipment_id is not None:
            query = query.where(ACTIONS.c.ShipmentID == shipment_id)
        with self.engine.connect() as conn:
            return int(conn.execute(query).scalar_one())

    def last_created(self, action: str, shipment_id: str) -> datetime | None:
        """When this action was last created for this shipment (for cooldowns)."""
        with self.engine.connect() as conn:
            value = conn.execute(
                select(func.max(ACTIONS.c.CreatedAt))
                .where(ACTIONS.c.Action == action)
                .where(ACTIONS.c.ShipmentID == shipment_id)
                .where(ACTIONS.c.Status != ActionStatus.CANCELLED.value)
            ).scalar_one_or_none()
            return value


class ControlStore:
    """Kill switch and dry run. The database can only switch them on over the YAML default:
    if either source says on, it is on (the safer reading)."""

    def __init__(self, engine: Engine, defaults: dict[ControlName, bool]) -> None:
        self.engine = engine
        self.defaults = defaults

    def is_on(self, name: ControlName) -> bool:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(CONTROL_FLAGS.c.Value).where(CONTROL_FLAGS.c.Name == name)
            ).scalar_one_or_none()
        return self.defaults.get(name, False) or bool(value)

    def set(self, name: ControlName, on: bool, actor: str, at: datetime) -> None:
        with self.engine.begin() as conn:
            exists = conn.execute(
                select(CONTROL_FLAGS.c.Name).where(CONTROL_FLAGS.c.Name == name)
            ).first()
            values = {"Value": int(on), "UpdatedBy": actor, "UpdatedAt": at}
            if exists:
                conn.execute(
                    update(CONTROL_FLAGS).where(CONTROL_FLAGS.c.Name == name).values(values)
                )
            else:
                conn.execute(insert(CONTROL_FLAGS), {"Name": name, **values})


def hour_before(now: datetime) -> datetime:
    return now - timedelta(hours=1)
