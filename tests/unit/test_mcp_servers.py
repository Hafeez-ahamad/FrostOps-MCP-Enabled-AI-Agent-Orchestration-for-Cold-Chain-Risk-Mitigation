"""MCP servers (spec section 11): server-side allow-lists per role, drafts, and the gate.

Tools are called in process through MCPServer.call_tool, exactly as a client's request would
arrive, so the allow-list and argument validation are the server's own.
"""

import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import insert, select, update

from engine.data.schema import INCIDENTS, PROPOSALS
from mcp_servers.servers import FACTORIES, McpDeps, Service, build_server, load_allow_lists
from tests.unit.harness import T0, Harness

INCIDENT = "INC-S1-01"
SERVICES: list[Service] = ["fleet", "planning", "sop", "action"]


def call(server: MCPServer, tool: str, **arguments: Any) -> Any:
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result  # type: ignore[union-attr]
    data = result.structured_content  # type: ignore[union-attr]
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]  # lists come wrapped
    return data


def tool_names(server: MCPServer) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


@pytest.fixture
def h() -> Harness:
    harness = Harness.build()
    harness.proposal("notify_driver_restart_cooling")  # records the rule hit (seq 7, valid)
    with harness.engine.begin() as conn:
        conn.execute(
            insert(INCIDENTS),
            {
                "IncidentID": INCIDENT,
                "ShipmentID": "S1",
                "DepotID": "DEP-LB",
                "CargoType": "FRESH",
                "Status": "open",
                "OpenedAt": T0,
                "LastHitAt": T0,
                "RuleIds": "R-TEMP-01",
                "HitCount": 1,
                "FirstSeqNo": 7,
                "LastSeqNo": 7,
                "RiskScore": 60.0,
                "UpdatedAt": T0,
            },
        )
    return harness


def deps(h: Harness) -> McpDeps:
    return McpDeps(h.engine, h.integrations, h.gate, h.executor, clock=lambda: T0)


def servers(h: Harness, role: str) -> dict[Service, MCPServer]:
    return {s: build_server(s, role, deps(h)) for s in SERVICES}


def stored(h: Harness, proposal_id: str) -> dict[str, Any]:
    with h.engine.connect() as conn:
        row = conn.execute(select(PROPOSALS).where(PROPOSALS.c.ProposalID == proposal_id))
        return dict(row.mappings().one())


# --- allow-lists -------------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(load_allow_lists()))
def test_each_role_lists_only_its_allow_listed_tools(h: Harness, role: str) -> None:
    allowed = load_allow_lists()[role]
    listed: set[str] = set()
    for service, server in servers(h, role).items():
        names = tool_names(server)
        assert names <= set(FACTORIES[service](deps(h)))
        listed |= names
    assert listed == allowed


@pytest.mark.parametrize(
    ("role", "service", "tool", "arguments"),
    [
        ("monitor", "planning", "create_proposal", {"incident_id": INCIDENT}),
        ("monitor", "action", "submit_action", {"proposal_id": "x"}),
        ("explainer", "action", "submit_action", {"proposal_id": "x"}),
        ("explainer", "planning", "create_proposal", {"incident_id": INCIDENT}),
        ("planner", "action", "submit_action", {"proposal_id": "x"}),
        ("dispatcher_agent", "planning", "create_proposal", {"incident_id": INCIDENT}),
    ],
)
def test_a_tool_outside_the_allow_list_does_not_exist(
    h: Harness, role: str, service: Service, tool: str, arguments: dict[str, Any]
) -> None:
    server = build_server(service, role, deps(h))
    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(server.call_tool(tool, arguments))


def test_unknown_role_gets_no_server(h: Harness) -> None:
    with pytest.raises(ValueError, match="unknown agent role"):
        build_server("fleet", "root", deps(h))


# --- drafts and the gate --------------------------------------------------------------------


def test_create_proposal_takes_its_evidence_from_the_database(h: Harness) -> None:
    planning = build_server("planning", "planner", deps(h))
    arguments = {
        "incident_id": INCIDENT,
        "action": "notify_driver_restart_cooling",
        "target": "S1",
        "rationale": "x" * 2000,
    }
    schema = next(t for t in asyncio.run(planning.list_tools()) if t.name == "create_proposal")
    assert "evidence" not in schema.input_schema["properties"]  # the caller cannot supply it

    out = call(planning, "create_proposal", **arguments)
    row = stored(h, out["proposal_id"])
    assert row["Status"] == "draft"
    assert row["ProposalHash"] == out["proposal_hash"]
    proposal = h.gate.proposals.proposal(out["proposal_id"])
    assert proposal is not None
    assert [(e.rule_id, e.seq, e.dq_status) for e in proposal.evidence] == [
        ("R-TEMP-01", 7, "valid")
    ]
    assert len(proposal.rationale) == 600
    assert proposal.planner == "llm"
    assert h.executor.actions.all() == []  # a draft does nothing


def test_create_proposal_for_an_unknown_incident_fails(h: Harness) -> None:
    planning = build_server("planning", "planner", deps(h))
    with pytest.raises(ToolError, match="unknown incident"):
        asyncio.run(
            planning.call_tool(
                "create_proposal",
                {"incident_id": "INC-NOPE", "action": "a", "target": "t", "rationale": "r"},
            )
        )


def _draft(h: Harness, action: str, target: str = "S1") -> str:
    planning = build_server("planning", "planner", deps(h))
    out = call(
        planning,
        "create_proposal",
        incident_id=INCIDENT,
        action=action,
        target=target,
        rationale="test",
    )
    return str(out["proposal_id"])


def test_submit_action_goes_through_the_gate_l3_waits_for_a_human(h: Harness) -> None:
    proposal_id = _draft(h, "escalate_tier2_manager")
    action = build_server("action", "dispatcher_agent", deps(h))
    out = call(action, "submit_action", proposal_id=proposal_id)
    assert out["outcome"] == "await_approval"
    assert stored(h, proposal_id)["Status"] == "awaiting_approval"
    assert h.executor.actions.all() == []


def test_submit_action_l2_executes_within_limits_and_l4_never(h: Harness) -> None:
    action = build_server("action", "dispatcher_agent", deps(h))
    l2 = call(action, "submit_action", proposal_id=_draft(h, "notify_driver_restart_cooling"))
    assert l2["outcome"] == "execute"
    assert [a.action for a in h.executor.actions.all()] == ["notify_driver_restart_cooling"]

    l4 = call(action, "submit_action", proposal_id=_draft(h, "reject_load"))
    assert l4["outcome"] == "rejected"
    unknown = call(action, "submit_action", proposal_id=_draft(h, "open_the_doors"))
    assert unknown["outcome"] == "rejected"
    assert "unknown_action" in unknown["reasons"]
    assert len(h.executor.actions.all()) == 1


def test_a_draft_is_submitted_only_once(h: Harness) -> None:
    proposal_id = _draft(h, "escalate_tier2_manager")
    action = build_server("action", "dispatcher_agent", deps(h))
    call(action, "submit_action", proposal_id=proposal_id)
    again = call(action, "submit_action", proposal_id=proposal_id)
    assert again["outcome"] == "duplicate"


def test_a_draft_edited_after_storing_is_rejected(h: Harness) -> None:
    proposal_id = _draft(h, "notify_driver_restart_cooling")
    row = stored(h, proposal_id)
    payload = json.loads(row["Payload"])
    payload["action"] = "divert_to_cold_storage"  # tampered in the database
    with h.engine.begin() as conn:
        conn.execute(
            update(PROPOSALS)
            .where(PROPOSALS.c.ProposalID == proposal_id)
            .values(Payload=json.dumps(payload))
        )
    action = build_server("action", "dispatcher_agent", deps(h))
    out = call(action, "submit_action", proposal_id=proposal_id)
    assert out["outcome"] == "rejected"
    assert "proposal_hash_mismatch" in out["reasons"]
    assert stored(h, proposal_id)["Status"] == "rejected"
    assert h.executor.actions.all() == []


def test_submitting_an_unknown_proposal_is_rejected(h: Harness) -> None:
    action = build_server("action", "dispatcher_agent", deps(h))
    out = call(action, "submit_action", proposal_id="PRP-NOPE")
    assert out["outcome"] == "rejected"
    assert out["reasons"] == ["unknown_proposal"]


# --- read tools -----------------------------------------------------------------------------


def test_fleet_and_sop_tools_answer_from_data(h: Harness) -> None:
    fleet = build_server("fleet", "monitor", deps(h))
    breaches = call(fleet, "list_active_breaches")
    assert [b["IncidentID"] for b in breaches] == [INCIDENT]
    hits = call(fleet, "get_rule_hits", shipment_id="S1")
    assert hits and hits[0]["RuleId"] == "R-TEMP-01"
    sop = build_server("sop", "planner", deps(h))
    clause = call(sop, "get_sop_clause", rule_id="R-TEMP-01")
    assert "4.0" in clause["text"]
