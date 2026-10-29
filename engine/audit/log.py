"""Append-only, hash-chained audit log. Fail closed.

Every decision, approval and action step is written here *before* the step takes effect. If
the write fails, `append` raises `AuditUnavailable` and the caller must not go ahead: the
gate rejects, and the executor leaves the action queued.

Each row stores the sha256 of the previous row's hash plus its own content, so editing or
deleting a row breaks the chain (`verify_chain`). The table is insert-only for every login.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import SQLAlchemyError

from engine.data.schema import AUDIT_LOG

GENESIS = "0" * 64


class AuditUnavailable(RuntimeError):
    """The audit write failed; the guarded step must not run."""


@dataclass(frozen=True)
class AuditEntry:
    at: datetime
    actor: str
    event_type: str
    proposal_id: str | None
    action_id: str | None
    detail: dict[str, Any]


def _row_hash(prev_hash: str, entry: AuditEntry) -> str:
    body = json.dumps(
        {
            "prev": prev_hash,
            "at": entry.at.isoformat(),
            "actor": entry.actor,
            "event": entry.event_type,
            "proposal": entry.proposal_id,
            "action": entry.action_id,
            "detail": entry.detail,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def append(
        self,
        event_type: str,
        actor: str,
        at: datetime,
        *,
        proposal_id: str | None = None,
        action_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        """Write one entry and return its hash. Raises AuditUnavailable on any failure."""
        entry = AuditEntry(at, actor, event_type, proposal_id, action_id, detail or {})
        try:
            with self.engine.begin() as conn:
                prev = conn.execute(
                    select(AUDIT_LOG.c.RowHash).order_by(AUDIT_LOG.c.AuditID.desc()).limit(1)
                ).scalar_one_or_none()
                row_hash = _row_hash(prev or GENESIS, entry)
                conn.execute(
                    insert(AUDIT_LOG),
                    {
                        "At": at,
                        "Actor": actor,
                        "EventType": event_type,
                        "ProposalID": proposal_id,
                        "ActionID": action_id,
                        "Detail": json.dumps(entry.detail, sort_keys=True, default=str),
                        "PrevHash": prev or GENESIS,
                        "RowHash": row_hash,
                    },
                )
        except SQLAlchemyError as exc:
            raise AuditUnavailable(f"audit write failed: {type(exc).__name__}") from exc
        return row_hash

    def entries(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(AUDIT_LOG).order_by(AUDIT_LOG.c.AuditID)).mappings()
            return [dict(r) for r in rows]


def verify_chain(rows: list[dict[str, Any]]) -> bool:
    """True if every row's hash matches its content and the previous row's hash."""
    prev = GENESIS
    for row in rows:
        entry = AuditEntry(
            at=row["At"],
            actor=row["Actor"],
            event_type=row["EventType"],
            proposal_id=row["ProposalID"],
            action_id=row["ActionID"],
            detail=json.loads(row["Detail"]),
        )
        if row["PrevHash"] != prev or row["RowHash"] != _row_hash(prev, entry):
            return False
        prev = row["RowHash"]
    return True
