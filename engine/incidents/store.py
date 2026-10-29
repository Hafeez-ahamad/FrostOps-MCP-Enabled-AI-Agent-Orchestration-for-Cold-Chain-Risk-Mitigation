"""Persist rule hits (append-only) and incidents (upsert by id) with parameterised queries."""

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select, update

from engine.data.schema import INCIDENTS, RULE_HITS
from engine.incidents.manager import Incident
from engine.rules.engine import RuleHit


def _hit_row(hit: RuleHit, evaluated_at: datetime) -> dict[str, Any]:
    return {
        "RuleId": hit.rule_id,
        "ShipmentID": hit.shipment_id,
        "MsgSeqNo": hit.seq,
        "ReadingTs": hit.reading_ts,
        "DqStatus": hit.dq_status,
        "SopRef": hit.sop_ref,
        "Observed": json.dumps(hit.observed, sort_keys=True),
        "Threshold": json.dumps(hit.threshold, sort_keys=True),
        "EvaluatedAt": evaluated_at,
    }


def save_rule_hits(conn: Connection, hits: Iterable[RuleHit], evaluated_at: datetime) -> int:
    rows = [_hit_row(h, evaluated_at) for h in hits]
    if rows:
        conn.execute(insert(RULE_HITS), rows)
    return len(rows)


def _incident_row(incident: Incident, updated_at: datetime) -> dict[str, Any]:
    return {
        "IncidentID": incident.incident_id,
        "ShipmentID": incident.shipment_id,
        "DepotID": incident.depot_id,
        "CargoType": incident.cargo_type,
        "Status": incident.status,
        "OpenedAt": incident.opened_at,
        "LastHitAt": incident.last_hit_at,
        "ClearedAt": incident.cleared_at,
        "RuleIds": ",".join(incident.rule_ids),
        "HitCount": incident.hit_count,
        "FirstSeqNo": incident.first_seq,
        "LastSeqNo": incident.last_seq,
        "RiskScore": incident.risk.score,
        "UpdatedAt": updated_at,
    }


def save_incidents(conn: Connection, incidents: Iterable[Incident], updated_at: datetime) -> int:
    """Insert new incidents and update existing ones. Incidents are never deleted."""
    rows = [_incident_row(i, updated_at) for i in incidents]
    if not rows:
        return 0
    ids = [r["IncidentID"] for r in rows]
    existing = set(
        conn.execute(
            select(INCIDENTS.c.IncidentID).where(INCIDENTS.c.IncidentID.in_(ids))
        ).scalars()
    )
    for row in rows:
        if row["IncidentID"] in existing:
            changes = {k: v for k, v in row.items() if k != "IncidentID"}
            conn.execute(
                update(INCIDENTS).where(INCIDENTS.c.IncidentID == row["IncidentID"]).values(changes)
            )
        else:
            conn.execute(insert(INCIDENTS), row)
    return len(rows)
