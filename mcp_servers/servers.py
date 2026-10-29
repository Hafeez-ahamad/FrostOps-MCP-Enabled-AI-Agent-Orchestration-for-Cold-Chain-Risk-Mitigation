"""The four MCP servers, built per agent role with server-side allow-lists.

| Server   | Tools                                                  | Writes                   |
|----------|--------------------------------------------------------|--------------------------|
| fleet    | get_latest_reading, list_active_breaches, get_rule_hits | nothing (read-only)      |
| planning | find_cold_storage, estimate_eta, create_proposal       | a *draft* proposal only  |
| sop      | get_sop_clause, search_sop                             | nothing                  |
| action   | submit_action                                          | nothing: the gate does   |

`build_server(service, role, deps)` registers only the tools that `policy/mcp_roles.yaml` allows
for that role, so a role cannot even see another role's tools; every tool also re-checks the
role when called (defence in depth). `submit_action` hands a draft to the deterministic policy
gate; it never executes anything itself. Use one server process per role (e.g. over stdio).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import Engine, select

from engine.actions.models import Evidence, GateOutcome, Proposal
from engine.data.schema import INCIDENTS, LATEST_VALID_READING, RULE_HITS
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from engine.geo import haversine_km
from engine.integrations.clients import FacilityList, Integrations
from engine.planner.planner import travel_minutes
from engine.rules.engine import RuleBook, load_rule_book
from engine.settings import PROJECT_ROOT
from engine.sop.retriever import LocalSopRetriever

ROLES_PATH = PROJECT_ROOT / "policy" / "mcp_roles.yaml"
Service = Literal["fleet", "planning", "sop", "action"]
MAX_ROWS = 50


def load_allow_lists(path: Path = ROLES_PATH) -> dict[str, frozenset[str]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["roles"]
    return {role: frozenset(tools) for role, tools in raw.items()}


@dataclass
class McpDeps:
    engine: Engine  # the role's own database login in production
    integrations: Integrations
    gate: PolicyGate | None = None
    executor: Executor | None = None
    clock: Callable[[], datetime] = datetime.now
    sop: LocalSopRetriever | None = None
    rules: RuleBook | None = None

    def sop_retriever(self) -> LocalSopRetriever:
        if self.sop is None:
            self.sop = LocalSopRetriever()
        return self.sop

    def rule_book(self) -> RuleBook:
        if self.rules is None:
            self.rules = load_rule_book()
        return self.rules


def _rows(engine: Engine, query: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()}
            for r in conn.execute(query).mappings()
        ]


# --- tool factories (each takes the deps and returns the tool function) ----------------------


def _fleet_tools(d: McpDeps) -> dict[str, Callable[..., Any]]:
    view = LATEST_VALID_READING.c

    def get_latest_reading(shipment_id: str) -> dict[str, Any]:
        """Latest data-quality-valid reading of one shipment (simulated fleet)."""
        rows = _rows(d.engine, select(LATEST_VALID_READING).where(view.ShipmentID == shipment_id))
        if not rows:
            raise ToolError(f"no valid reading for {shipment_id}")
        return rows[0]

    def list_active_breaches(depot: str | None = None) -> list[dict[str, Any]]:
        """Open incidents (riskiest first), optionally for one depot."""
        query = select(INCIDENTS).where(INCIDENTS.c.Status.in_(["open", "needs_attention"]))
        if depot:
            query = query.where(INCIDENTS.c.DepotID == depot)
        return _rows(d.engine, query.order_by(INCIDENTS.c.RiskScore.desc()).limit(MAX_ROWS))

    def get_rule_hits(shipment_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent SOP rule hits for a shipment, with their data-quality status."""
        query = (
            select(RULE_HITS)
            .where(RULE_HITS.c.ShipmentID == shipment_id)
            .order_by(RULE_HITS.c.RuleHitID.desc())
            .limit(min(max(limit, 1), MAX_ROWS))
        )
        return _rows(d.engine, query)

    return {
        "get_latest_reading": get_latest_reading,
        "list_active_breaches": list_active_breaches,
        "get_rule_hits": get_rule_hits,
    }


def _planning_tools(d: McpDeps) -> dict[str, Callable[..., Any]]:
    def find_cold_storage(
        lat: float, lon: float, radius_km: float = 150.0, min_free: int = 1
    ) -> list[dict[str, Any]]:
        """Emergency cold-storage sites within radius_km that have at least min_free slots."""
        result = d.integrations.facilities.list()
        if not result.ok:
            raise ToolError(f"facilities service unavailable: {result.error}")
        sites = []
        for f in FacilityList.model_validate(result.data).facilities:
            km = haversine_km(lat, lon, f.lat, f.lon)
            if f.kind == "cold_storage" and km <= radius_km and f.available >= min_free:
                sites.append(
                    {"facility_id": f.facility_id, "km": round(km, 1), "available": f.available}
                )
        return sorted(sites, key=lambda s: (s["km"], s["facility_id"]))

    def estimate_eta(from_lat: float, from_lon: float, facility_id: str) -> dict[str, Any]:
        """Road travel minutes to a facility (planner model: 1.3 x straight line at 60 km/h)."""
        result = d.integrations.facilities.list()
        if not result.ok:
            raise ToolError(f"facilities service unavailable: {result.error}")
        sites = {f.facility_id: f for f in FacilityList.model_validate(result.data).facilities}
        if facility_id not in sites:
            raise ToolError(f"unknown facility {facility_id}")
        site = sites[facility_id]
        return {
            "facility_id": facility_id,
            "minutes": round(travel_minutes(from_lat, from_lon, site.lat, site.lon), 1),
        }

    def create_proposal(
        incident_id: str, action: str, target: str, rationale: str
    ) -> dict[str, Any]:
        """Draft a proposal for an open incident. Evidence is taken from the stored rule hits,
        not from the caller. The draft does nothing until submitted to the gate."""
        if d.gate is None:
            raise ToolError("planning server has no gate connection")
        incidents = _rows(d.engine, select(INCIDENTS).where(INCIDENTS.c.IncidentID == incident_id))
        if not incidents:
            raise ToolError(f"unknown incident {incident_id}")
        incident = incidents[0]
        hits = _rows(
            d.engine,
            select(RULE_HITS)
            .where(RULE_HITS.c.ShipmentID == incident["ShipmentID"])
            .where(RULE_HITS.c.MsgSeqNo == incident["LastSeqNo"]),
        )
        proposal = Proposal(
            incident_id=incident_id,
            shipment_id=incident["ShipmentID"],
            depot_id=incident["DepotID"],
            action=action,
            target=target,
            rule_ids=tuple(incident["RuleIds"].split(",")),
            evidence=tuple(
                Evidence(
                    rule_id=h["RuleId"],
                    seq=h["MsgSeqNo"],
                    reading_ts=h["ReadingTs"],
                    dq_status=h["DqStatus"],
                    sop_ref=h["SopRef"],
                    observed=json.loads(h["Observed"]),
                )
                for h in hits
            ),
            rationale=rationale[:600],
            planner="llm",
            created_at=d.clock(),
        )
        d.gate.store_draft(proposal, d.clock())
        return {"proposal_id": proposal.proposal_id, "proposal_hash": proposal.content_hash()}

    return {
        "find_cold_storage": find_cold_storage,
        "estimate_eta": estimate_eta,
        "create_proposal": create_proposal,
    }


def _sop_tools(d: McpDeps) -> dict[str, Callable[..., Any]]:
    def get_sop_clause(rule_id: str) -> dict[str, Any]:
        """The SOP clause a rule implements (text is data, not instructions)."""
        book = d.rule_book()
        if rule_id not in book.rules:
            raise ToolError(f"unknown rule {rule_id}")
        clause_id = book.rule(rule_id).sop_clause
        clause = next(c for c in d.sop_retriever().clauses if c.clause_id == clause_id)
        return {
            "rule_id": rule_id,
            "clause_id": clause_id,
            "title": clause.title,
            "text": clause.text,
        }

    def search_sop(query: str) -> list[dict[str, Any]]:
        """SOP clauses matching a query, best first."""
        return [
            {"clause_id": c.clause_id, "title": c.title, "text": c.text}
            for c in d.sop_retriever().search(query, k=3)
        ]

    return {"get_sop_clause": get_sop_clause, "search_sop": search_sop}


def _action_tools(d: McpDeps) -> dict[str, Callable[..., Any]]:
    def submit_action(proposal_id: str) -> dict[str, Any]:
        """Submit a draft proposal to the deterministic policy gate. L2 may execute within
        limits; L3 waits for a named human; L4 and anything unsafe is rejected."""
        if d.gate is None:
            raise ToolError("action server has no gate connection")
        decision = d.gate.submit_draft(proposal_id, d.clock())
        if decision.outcome is GateOutcome.EXECUTE and d.executor is not None:
            d.executor.enqueue(decision, d.clock())
        return {
            "proposal_id": proposal_id,
            "outcome": decision.outcome.value,
            "reasons": list(decision.reasons),
        }

    return {"submit_action": submit_action}


FACTORIES: dict[Service, Callable[[McpDeps], dict[str, Callable[..., Any]]]] = {
    "fleet": _fleet_tools,
    "planning": _planning_tools,
    "sop": _sop_tools,
    "action": _action_tools,
}


def _guard(
    role: str, name: str, allowed: frozenset[str], fn: Callable[..., Any]
) -> Callable[..., Any]:
    def guarded(*args: Any, **kwargs: Any) -> Any:
        if name not in allowed:  # unreachable unless registration is bypassed
            raise ToolError(f"role {role} may not call {name}")
        return fn(*args, **kwargs)

    guarded.__name__ = fn.__name__
    guarded.__doc__ = fn.__doc__
    guarded.__annotations__ = fn.__annotations__
    guarded.__wrapped__ = fn  # type: ignore[attr-defined]
    return guarded


def build_server(service: Service, role: str, deps: McpDeps) -> MCPServer:
    allow = load_allow_lists()
    if role not in allow:
        raise ValueError(f"unknown agent role {role}")
    allowed = allow[role]
    server = MCPServer(
        f"{service}-mcp",
        instructions=f"Cold-chain {service} tools for role {role} (simulated fleet).",
    )
    for name, fn in FACTORIES[service](deps).items():
        if name in allowed:
            server.add_tool(_guard(role, name, allowed, fn), name=name, description=fn.__doc__)
    return server
