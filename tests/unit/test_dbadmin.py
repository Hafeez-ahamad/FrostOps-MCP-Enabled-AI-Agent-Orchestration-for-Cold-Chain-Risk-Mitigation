"""The db-init script runner: batching, variables, and the scripts themselves."""

import re

import pytest
from pydantic import SecretStr

from engine.dbadmin import DB_DIR, SCRIPTS, script_variables, split_batches, substitute
from engine.settings import Settings


def test_split_on_go_lines_only() -> None:
    sql = "SELECT 1;\nGO\nSELECT 'GO here';\n  go  \nSELECT 3;"
    assert split_batches(sql) == ["SELECT 1;", "SELECT 'GO here';", "SELECT 3;"]


def test_variables_are_escaped_as_string_literals() -> None:
    sql = "CREATE LOGIN x WITH PASSWORD = N'$(AGENT_PASSWORD)';"
    out = substitute(sql, {"AGENT_PASSWORD": "it's;--"})
    assert out == "CREATE LOGIN x WITH PASSWORD = N'it''s;--';"


def test_missing_variable_is_an_error() -> None:
    with pytest.raises(KeyError, match="AGENT_PASSWORD"):
        substitute("N'$(AGENT_PASSWORD)'", {})


def test_db_init_requires_every_password(settings: Settings) -> None:
    with pytest.raises(RuntimeError, match="AGENT_PASSWORD"):
        script_variables(settings)
    full = settings.model_copy(
        update={
            "sql_agent_password": SecretStr("a"),
            "sql_ingest_password": SecretStr("b"),
            "sql_console_password": SecretStr("c"),
            "sql_monitor_password": SecretStr("d"),
            "sql_planner_password": SecretStr("e"),
            "sql_gate_password": SecretStr("f"),
        }
    )
    with pytest.raises(RuntimeError, match="EXECUTOR_PASSWORD"):
        script_variables(full)
    full = full.model_copy(update={"sql_executor_password": SecretStr("g")})
    assert script_variables(full) == {
        "AGENT_PASSWORD": "a",
        "INGEST_PASSWORD": "b",
        "CONSOLE_PASSWORD": "c",
        "MONITOR_PASSWORD": "d",
        "PLANNER_PASSWORD": "e",
        "GATE_PASSWORD": "f",
        "EXECUTOR_PASSWORD": "g",
    }


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_target_coldchaindb_and_hold_no_secrets(name: str) -> None:
    sql = (DB_DIR / name).read_text(encoding="utf-8")
    assert "USE ColdChainDB;" in sql
    assert "USE master" not in sql
    assert not re.search(r"PASSWORD\s*=\s*N?'[^$]", sql), "literal password in SQL script"
    names = ["AGENT", "INGEST", "CONSOLE", "MONITOR", "PLANNER", "GATE", "EXECUTOR"]
    substitute(sql, {f"{n}_PASSWORD": "x" for n in names})


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_written_to_be_rerunnable(name: str) -> None:
    """Every CREATE is guarded (IF ... IS NULL / NOT EXISTS) or is CREATE OR ALTER."""
    for batch in split_batches((DB_DIR / name).read_text(encoding="utf-8")):
        for line in batch.splitlines():
            stripped = line.strip().upper()
            if not stripped.startswith("CREATE ") or stripped.startswith("CREATE OR ALTER"):
                continue
            guarded = "IF " in batch.upper() or stripped.startswith("CREATE SECURITY POLICY")
            assert guarded, f"{name}: unguarded statement: {line.strip()}"
