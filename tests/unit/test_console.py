"""The console is a client of the dispatch API: it renders offline, and with a fake API it shows
the screens and sends decisions bound to the proposal hash. No LLM, database or key is needed."""

from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "console" / "app.py"
HASH = "a" * 64
PROPOSAL = {
    "ProposalID": "PRP-1",
    "ProposalHash": HASH,
    "IncidentID": "INC-S1-01",
    "ShipmentID": "S1",
    "DepotID": "DEP-LB",
    "Action": "divert_to_cold_storage",
    "Target": "CS-VENTURA",
    "Level": "L3",
    "Status": "awaiting_approval",
    "Payload": "{}",
}
INCIDENT = {
    "IncidentID": "INC-S1-01",
    "ShipmentID": "S1",
    "DepotID": "DEP-LB",
    "CargoType": "FRESH",
    "Status": "open",
    "RuleIds": "R-TEMP-01,R-ETA-01",
    "RiskScore": 72.5,
    "HitCount": 3,
    "OpenedAt": "2026-03-02T07:10:00",
}


class FakeApi:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, **params: Any) -> Any:
        if path == "/health":
            return {
                "simulated_time": "2026-03-02T10:00:00",
                "configuration": "C0",
                "kill_switch": False,
                "dry_run": False,
                "alert_only": False,
            }
        if path == "/incidents":
            return [INCIDENT]
        if path == "/proposals":
            return [PROPOSAL]
        if path == "/admin/policy":
            return {"policy_hash": "b" * 64, "history": [], "yaml_text": "version: 1"}
        if path == "/actions":
            return []
        if path == "/audit":
            return {"chain_valid": True, "entries": []}
        return {}

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        self.posts.append((path, body))
        if path.endswith("/approve"):
            return {"outcome": "execute", "reasons": []}
        return {"kill_switch": True}


def app_with(fake: FakeApi | None = None) -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=60)
    if fake is not None:
        app.session_state["api_client"] = fake
    return app.run()


def test_console_renders_without_backends() -> None:
    app = app_with()
    assert not app.exception
    assert app.title[0].value == "Cold-Chain Dispatch Console"
    assert "Simulated fleet" in app.caption[0].value
    assert "token" in app.info[0].value.lower()


def test_incident_board_lists_incidents_from_the_api() -> None:
    app = app_with(FakeApi())
    assert not app.exception
    assert app.metric[1].value == "C0"
    assert "Active incidents (1)" in app.subheader[0].value
    assert app.dataframe[0].value["IncidentID"].tolist() == ["INC-S1-01"]


def test_approve_sends_the_hash_of_the_proposal_shown() -> None:
    fake = FakeApi()
    app = app_with(fake)
    app.sidebar.radio[0].set_value("Approval queue").run()
    assert "Waiting for a human (1)" in app.subheader[0].value
    app.text_input(key="reason-PRP-1").input("confirmed with depot").run()
    app.button(key="ok-PRP-1").click().run()
    assert not app.exception
    assert fake.posts == [
        ("/proposals/PRP-1/approve", {"proposal_hash": HASH, "reason": "confirmed with depot"})
    ]
    assert "Gate: execute" in app.success[0].value


def test_kill_switch_button_calls_the_api() -> None:
    fake = FakeApi()
    app = app_with(fake)
    app.sidebar.radio[0].set_value("Autonomy panel").run()
    app.button[0].click().run()
    assert ("/admin/killswitch", {"on": True}) in fake.posts
