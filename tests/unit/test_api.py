"""The dispatch API: authentication, roles, depot scope, approvals, controls, policy, audit.

Runs on the smoke scenario in memory (simulated fleet, C0, no LLM). At 10 simulated hours there
are L3 proposals waiting for a human in DEP-SD and DEP-ONT.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import User, UserDirectory, token_hash
from api.service import EngineService
from engine.actions.policy import ACTIONS_PATH
from engine.data.memory_db import in_memory_engine
from engine.runtime import Runtime
from sim.generator import SimulationOutput
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import Scenario

USERS = {
    "dana": User("dana", "dispatcher", frozenset({"DEP-SD"})),
    "sam": User("sam", "supervisor", None),
    "ana": User("ana", "auditor", None),
    "adi": User("adi", "admin", None),
}
DIVERT = "PRP-INC-S0007-00093-01-divert_to_cold_storage-CS-ESCONDIDO"


def make_service(smoke: SimulationOutput, scenario: Scenario, hours: float) -> EngineService:
    mock = MockWorld(now=scenario.start, events=smoke.events)
    runtime = Runtime.build(in_memory_engine(), in_process_integrations(mock, FaultInjector()))
    service = EngineService(runtime, smoke, mock, scenario.start)
    if hours:
        service.advance(hours * 60)
    return service


@dataclass
class Api:
    service: EngineService
    http: TestClient

    def as_(self, user: str) -> dict[str, str]:
        return {"Authorization": f"Bearer token-{user}"}

    def get(self, path: str, user: str, **params: object) -> object:
        response = self.http.get(path, headers=self.as_(user), params=params)  # type: ignore[arg-type]
        assert response.status_code == 200, response.text
        return response.json()


def build_api(service: EngineService) -> Api:
    users = UserDirectory()
    for user in USERS.values():
        users.add(user, token_hash(f"token-{user.user_id}"))
    chat = lambda message, thread, user: f"echo {message}"  # noqa: E731
    return Api(service, TestClient(create_app(service, users, chat)))


@pytest.fixture(scope="module")
def api(smoke: SimulationOutput, smoke_scenario: Scenario) -> Iterator[Api]:
    yield build_api(make_service(smoke, smoke_scenario, hours=10))


@pytest.fixture
def fresh(smoke: SimulationOutput, smoke_scenario: Scenario) -> Api:
    """No readings processed yet: for tests that change policy or controls."""
    return build_api(make_service(smoke, smoke_scenario, hours=0))


# --- authentication and roles --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/incidents", "/proposals", "/actions", "/audit"])
def test_requests_without_a_valid_token_are_401(api: Api, path: str) -> None:
    assert api.http.get(path).status_code == 401
    assert api.http.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body", "allowed"),
    [
        ("get", "/audit", None, {"sam", "ana", "adi"}),
        ("post", "/admin/killswitch", {"on": False}, {"sam", "adi"}),
        ("post", "/admin/dry-run", {"on": False}, {"sam", "adi"}),
        ("put", "/admin/policy", {"yaml_text": "x" * 20, "reason": "test"}, {"sam", "adi"}),
        ("post", "/admin/advance", {"minutes": 1}, {"sam", "adi"}),
        ("post", f"/proposals/{DIVERT}/reject", {"proposal_hash": "0" * 64}, {"dana", "sam"}),
    ],
)
def test_each_route_is_forbidden_to_roles_without_the_permission(
    fresh: Api, method: str, path: str, body: dict[str, object] | None, allowed: set[str]
) -> None:
    for user in USERS:
        response = getattr(fresh.http, method)(
            path, headers=fresh.as_(user), **({"json": body} if body else {})
        )
        if user in allowed:
            assert response.status_code != 403, (user, path)
        else:
            assert response.status_code == 403, (user, path)


def test_dispatcher_sees_only_their_depots(api: Api) -> None:
    everything = api.get("/incidents", "sam")
    mine = api.get("/incidents", "dana")
    assert isinstance(everything, list) and isinstance(mine, list)
    assert {i["DepotID"] for i in everything} > {"DEP-SD"}
    assert mine and {i["DepotID"] for i in mine} == {"DEP-SD"}
    queue = api.get("/proposals", "dana", status="pending")
    assert isinstance(queue, list) and queue
    assert {p["DepotID"] for p in queue} == {"DEP-SD"}
    other = next(i for i in everything if i["DepotID"] != "DEP-SD")
    response = api.http.get(f"/incidents/{other['IncidentID']}", headers=api.as_("dana"))
    assert response.status_code == 404


def test_dispatcher_cannot_decide_a_proposal_outside_their_depots(api: Api) -> None:
    queue = api.get("/proposals", "sam", status="pending")
    assert isinstance(queue, list)
    other = next(p for p in queue if p["DepotID"] != "DEP-SD")
    response = api.http.post(
        f"/proposals/{other['ProposalID']}/approve",
        headers=api.as_("dana"),
        json={"proposal_hash": other["ProposalHash"]},
    )
    assert response.status_code == 404


# --- approvals -------------------------------------------------------------------------------


def test_approval_with_a_wrong_hash_is_rejected_and_the_right_hash_executes(api: Api) -> None:
    queue = api.get("/proposals", "dana", status="pending")
    assert isinstance(queue, list)
    divert = next(p for p in queue if p["ProposalID"] == DIVERT)

    wrong = api.http.post(
        f"/proposals/{DIVERT}/approve", headers=api.as_("dana"), json={"proposal_hash": "f" * 64}
    ).json()
    assert wrong["outcome"] == "rejected"
    assert "proposal_hash_mismatch" in wrong["reasons"]

    right = api.http.post(
        f"/proposals/{DIVERT}/approve",
        headers=api.as_("dana"),
        json={"proposal_hash": divert["ProposalHash"], "reason": "facility confirmed"},
    ).json()
    assert right["outcome"] == "execute"
    api.http.post("/admin/advance", headers=api.as_("sam"), json={"minutes": 5})
    actions = api.get("/actions", "dana")
    assert isinstance(actions, list)
    done = [a for a in actions if a["proposal_id"] == DIVERT]
    assert [a["status"] for a in done] == ["succeeded"]

    again = api.http.post(
        f"/proposals/{DIVERT}/approve",
        headers=api.as_("sam"),
        json={"proposal_hash": divert["ProposalHash"]},
    ).json()
    assert again["outcome"] == "rejected"  # an approval cannot be reused or repeated


# --- controls -----------------------------------------------------------------------------------


def test_kill_switch_stops_the_executor_within_one_poll(fresh: Api) -> None:
    on = fresh.http.post("/admin/killswitch", headers=fresh.as_("sam"), json={"on": True})
    assert on.json() == {"kill_switch": True}
    assert fresh.get("/health", "sam")["kill_switch"] is True  # type: ignore[index]
    fresh.http.post("/admin/advance", headers=fresh.as_("sam"), json={"minutes": 10 * 60})
    assert fresh.get("/actions", "sam") == []
    decisions = fresh.service.runtime.dispatcher.decisions
    assert decisions and all(d.outcome.value != "execute" for d in decisions)
    assert all("kill_switch" in d.reasons for d in decisions if d.outcome.value == "rejected")
    audit = fresh.get("/audit", "ana", event_type="kill_switch_set")
    assert audit["entries"][0]["Actor"] == "supervisor:sam"  # type: ignore[index]


# --- policy -------------------------------------------------------------------------------------


def test_policy_update_cannot_lower_autonomy_level(fresh: Api) -> None:
    looser = ACTIONS_PATH.read_text(encoding="utf-8").replace(
        "  divert_to_cold_storage:\n    level: L3", "  divert_to_cold_storage:\n    level: L2"
    )
    assert looser != ACTIONS_PATH.read_text(encoding="utf-8")
    response = fresh.http.put(
        "/admin/policy",
        headers=fresh.as_("adi"),
        json={"yaml_text": looser, "reason": "let diversions run alone"},
    )
    assert response.status_code == 422
    assert "loosens autonomy" in response.json()["detail"]
    current = fresh.get("/admin/policy", "adi")
    assert [h["Version"] for h in current["history"]] == [1]  # type: ignore[index]  # startup only
    assert fresh.service.runtime.gate.catalog.get("divert_to_cold_storage").level.value == "L3"  # type: ignore[union-attr]


def test_policy_update_cannot_drop_an_l2_precondition(fresh: Api) -> None:
    looser = ACTIONS_PATH.read_text(encoding="utf-8").replace(
        "[rule_breach_confirmed, data_quality_ok, driver_reachable]",
        "[rule_breach_confirmed, data_quality_ok]",
    )
    response = fresh.http.put(
        "/admin/policy", headers=fresh.as_("adi"), json={"yaml_text": looser, "reason": "faster"}
    )
    assert response.status_code == 422
    assert "loses preconditions" in response.json()["detail"]


def test_stricter_policy_update_is_accepted_versioned_and_applied(fresh: Api) -> None:
    stricter = (
        ACTIONS_PATH.read_text(encoding="utf-8")
        .replace("version: 1", "version: 2")
        .replace(
            "  create_incident_ticket:\n    level: L2",
            "  create_incident_ticket:\n    level: L3\n    approver_roles: [supervisor]",
        )
    )
    response = fresh.http.put(
        "/admin/policy",
        headers=fresh.as_("sam"),
        json={"yaml_text": stricter, "reason": "tickets need a human this week"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    runtime = fresh.service.runtime
    for catalog in (runtime.gate.catalog, runtime.executor.catalog, runtime.catalog):
        assert catalog.get("create_incident_ticket").level.value == "L3"  # type: ignore[union-attr]
    history = fresh.get("/admin/policy", "ana")["history"]  # type: ignore[index]
    assert [(h["Version"], h["Author"]) for h in history] == [
        (1, "system"),
        (2, "supervisor:sam"),
    ]
    audit = fresh.get("/audit", "ana", event_type="policy_updated")
    assert audit["entries"][0]["Actor"] == "supervisor:sam"  # type: ignore[index]

    fresh.http.post("/admin/advance", headers=fresh.as_("sam"), json={"minutes": 10 * 60})
    tickets = [d for d in runtime.dispatcher.decisions if d.action == "create_incident_ticket"]
    assert tickets and all(d.outcome.value != "execute" for d in tickets)
    assert all(d.policy_version == 2 for d in tickets)


def test_policy_update_needs_a_higher_version(fresh: Api) -> None:
    response = fresh.http.put(
        "/admin/policy",
        headers=fresh.as_("adi"),
        json={"yaml_text": ACTIONS_PATH.read_text(encoding="utf-8"), "reason": "same version"},
    )
    assert response.status_code == 422
    assert "must be higher" in response.json()["detail"]
    assert len(fresh.get("/admin/policy", "adi")["history"]) == 1  # type: ignore[index]


def test_invalid_policy_is_rejected(fresh: Api) -> None:
    response = fresh.http.put(
        "/admin/policy",
        headers=fresh.as_("adi"),
        json={"yaml_text": "version: 9\nactions: {}\n", "reason": "oops"},
    )
    assert response.status_code == 422


# --- audit, health, metrics, chat ----------------------------------------------------------------


def test_audit_export_has_a_valid_chain(api: Api) -> None:
    export = api.get("/audit", "ana", limit=5000)
    assert isinstance(export, dict)
    assert export["chain_valid"] is True
    assert export["entries"]
    assert {e["EventType"] for e in export["entries"]} >= {"gate_decision", "action_executing"}


def test_health_needs_no_token_and_reports_the_mode(api: Api) -> None:
    body = api.http.get("/health").json()
    assert body["status"] == "ok"
    assert body["alert_only"] is False
    assert body["configuration"] == "C0"
    assert body["simulated_time"] == api.service.now.isoformat()


def test_metrics_exposes_the_engine_counters(api: Api) -> None:
    response = api.http.get("/metrics")
    assert response.status_code == 200
    text = response.text
    for name in (
        "coldchain_incidents_opened_total",
        "coldchain_gate_decisions_total",
        "coldchain_actions_total",
        "coldchain_approval_queue_depth",
        "coldchain_kill_switch",
        "coldchain_alert_only_mode",
    ):
        assert name in text
    assert 'coldchain_actions_total{action="create_incident_ticket",status="succeeded"}' in text


def test_chat_route_needs_a_token_and_returns_a_thread(api: Api) -> None:
    assert api.http.post("/chat", json={"message": "hi"}).status_code == 401
    body = api.http.post("/chat", headers=api.as_("dana"), json={"message": "hi"}).json()
    assert body["answer"] == "echo hi"
    assert body["thread_id"].startswith("dana-")


def test_chat_route_is_503_when_the_model_is_unreachable(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    def broken(message: str, thread: str, user: User) -> str:
        raise ConnectionError("connection refused")

    users = UserDirectory()
    users.add(USERS["dana"], token_hash("token-dana"))
    client = TestClient(create_app(make_service(smoke, smoke_scenario, hours=0), users, broken))
    response = client.post(
        "/chat", headers={"Authorization": "Bearer token-dana"}, json={"message": "hi"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Chat model unavailable: connection refused"}


def test_advance_moves_the_simulated_clock(fresh: Api) -> None:
    before = fresh.service.now
    body = fresh.http.post("/admin/advance", headers=fresh.as_("adi"), json={"minutes": 30}).json()
    assert body["simulated_time"] == (before + timedelta(minutes=30)).isoformat()
