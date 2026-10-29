"""Apply the db/*.sql scripts in order: python -m engine.dbadmin init

Every script is written to be re-runnable. Scripts use sqlcmd-style `GO` batch separators and
`$(NAME)` variables; passwords are substituted here from settings and escaped as T-SQL string
literals, so they are never written to a file or passed on a command line.
"""

import argparse
import re

from sqlalchemy import Engine

from engine.db import build_engine
from engine.settings import PROJECT_ROOT, Settings, load_settings

DB_DIR = PROJECT_ROOT / "db"
SCRIPTS = ["raw_schema.sql", "ops_schema.sql", "roles.sql"]
_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)
_VARIABLE = re.compile(r"\$\(([A-Z_][A-Z0-9_]*)\)")


def split_batches(sql: str) -> list[str]:
    return [batch.strip() for batch in _GO.split(sql) if batch.strip()]


def substitute(sql: str, variables: dict[str, str]) -> str:
    """Replace $(NAME). Values go inside N'...' literals, so single quotes are doubled."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise KeyError(f"SQL script needs ${{{name}}} but it was not provided")
        return variables[name].replace("'", "''")

    return _VARIABLE.sub(replace, sql)


def script_variables(settings: Settings) -> dict[str, str]:
    values = {
        "AGENT_PASSWORD": settings.sql_agent_password,
        "INGEST_PASSWORD": settings.sql_ingest_password,
        "CONSOLE_PASSWORD": settings.sql_console_password,
        "MONITOR_PASSWORD": settings.sql_monitor_password,
        "PLANNER_PASSWORD": settings.sql_planner_password,
        "GATE_PASSWORD": settings.sql_gate_password,
        "EXECUTOR_PASSWORD": settings.sql_executor_password,
    }
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise RuntimeError(f"set these in .env before db-init: {missing}")
    return {name: value.get_secret_value() for name, value in values.items() if value is not None}


def run_script(engine: Engine, sql: str, variables: dict[str, str]) -> int:
    batches = split_batches(substitute(sql, variables))
    with engine.connect() as conn:
        for batch in batches:
            conn.exec_driver_sql(batch)
    return len(batches)


def init_database(settings: Settings) -> None:
    # Connect to master: ColdChainDB may not exist yet. Autocommit because CREATE DATABASE
    # cannot run inside a transaction. Each script switches to ColdChainDB itself.
    engine = build_engine(settings, "admin", database="master", isolation_level="AUTOCOMMIT")
    variables = script_variables(settings)
    for name in SCRIPTS:
        count = run_script(engine, (DB_DIR / name).read_text(encoding="utf-8"), variables)
        print(f"applied db/{name} ({count} batches)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init"])
    parser.parse_args()
    init_database(load_settings())


if __name__ == "__main__":
    main()
