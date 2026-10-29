"""Connection settings that matter for security, checked without a database."""

from pydantic import SecretStr
from sqlalchemy.pool import NullPool

from engine.db import build_engine
from engine.settings import Settings


def _with_passwords(settings: Settings) -> Settings:
    secret = SecretStr("not-a-real-password")
    return settings.model_copy(
        update={
            "sql_agent_password": secret,
            "sql_ingest_password": secret,
            "sql_console_password": secret,
            "mssql_sa_password": secret,
        }
    )


def test_console_connections_are_never_pooled(settings: Settings) -> None:
    """A pooled connection would carry one dispatcher's depot scope to the next request."""
    engine = build_engine(_with_passwords(settings), "console")
    assert isinstance(engine.pool, NullPool)


def test_each_login_uses_its_own_user(settings: Settings) -> None:
    s = _with_passwords(settings)
    users = {
        login: build_engine(s, login).url.query["odbc_connect"]
        for login in ("agent", "ingest", "console")
    }
    assert "UID=ccx_reader;" in users["agent"]
    assert "UID=ccx_ingest;" in users["ingest"]
    assert "UID=ccx_console;" in users["console"]
