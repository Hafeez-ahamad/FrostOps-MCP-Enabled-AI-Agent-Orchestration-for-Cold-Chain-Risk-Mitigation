"""SQL Server connections, one login per purpose.

Only parameterised SQLAlchemy queries run on these engines. No LLM-written SQL.
"""

from typing import Literal
from urllib.parse import quote_plus

from pydantic import SecretStr
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.pool import NullPool

from engine.settings import Settings

Login = Literal["agent", "ingest", "console", "monitor", "planner", "gate", "executor", "admin"]

# login -> (settings field for the user, settings field for the password, env var to set)
_LOGINS: dict[Login, tuple[str, str, str]] = {
    "agent": ("sql_agent_user", "sql_agent_password", "SQL_AGENT_PASSWORD"),
    "ingest": ("sql_ingest_user", "sql_ingest_password", "SQL_INGEST_PASSWORD"),
    "console": ("sql_console_user", "sql_console_password", "SQL_CONSOLE_PASSWORD"),
    "monitor": ("sql_monitor_user", "sql_monitor_password", "SQL_MONITOR_PASSWORD"),
    "planner": ("sql_planner_user", "sql_planner_password", "SQL_PLANNER_PASSWORD"),
    "gate": ("sql_gate_user", "sql_gate_password", "SQL_GATE_PASSWORD"),
    "executor": ("sql_executor_user", "sql_executor_password", "SQL_EXECUTOR_PASSWORD"),
    "admin": ("sql_admin_user", "mssql_sa_password", "MSSQL_SA_PASSWORD"),
}


def _odbc_value(value: str) -> str:
    # ODBC values containing ; or } must be wrapped in braces, with } doubled.
    return "{" + value.replace("}", "}}") + "}"


def odbc_connection_string(
    settings: Settings, login: Login = "agent", database: str | None = None
) -> str:
    user_field, password_field, env_var = _LOGINS[login]
    password: SecretStr | None = getattr(settings, password_field)
    if password is None:
        raise RuntimeError(f"{env_var} is not set; see .env.example")
    parts = [
        "DRIVER={ODBC Driver 18 for SQL Server}",
        f"SERVER={settings.sql_server_host},{settings.sql_server_port}",
        f"DATABASE={database or settings.sql_database}",
        f"UID={getattr(settings, user_field)}",
        f"PWD={_odbc_value(password.get_secret_value())}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if settings.sql_trust_server_certificate else 'no'}",
    ]
    return ";".join(parts) + ";"


def build_engine(
    settings: Settings, login: Login, database: str | None = None, **kwargs: object
) -> Engine:
    """Create (but do not connect) an engine for one login.

    The console login is never pooled: its depot scope lives in SESSION_CONTEXT on the
    connection, and a pooled connection would carry one dispatcher's scope to the next.
    """
    url = "mssql+pyodbc:///?odbc_connect=" + quote_plus(
        odbc_connection_string(settings, login, database)
    )
    if login == "console":
        return create_engine(url, poolclass=NullPool, **kwargs)
    return create_engine(url, pool_pre_ping=True, **kwargs)


def build_agent_engine(settings: Settings) -> Engine:
    return build_engine(settings, "agent")


def set_depot_scope(conn: Connection, depot_ids: list[str]) -> None:
    """Limit this connection to the given depots (row-level security, db/roles.sql).

    read_only=1 stops the session from changing its own scope later.
    """
    scope = "|" + "|".join(depot_ids) + "|"
    conn.execute(
        text("EXEC sp_set_session_context @key = N'depot_ids', @value = :scope, @read_only = 1"),
        {"scope": scope},
    )
