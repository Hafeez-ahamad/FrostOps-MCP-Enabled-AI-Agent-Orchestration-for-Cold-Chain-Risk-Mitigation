"""Runtime settings, read from environment variables (and `.env` if present).

Nothing here connects to anything: building settings only reads configuration.
"""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOP_PATH = PROJECT_ROOT / "data" / "policy" / "cold_chain_sop.md"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # LLM backend: local Ollama by default, OpenAI as the one hosted option.
    agent_llm: Literal["OLLAMA", "OPENAI"] = "OLLAMA"
    ollama_model: str = "qwen2.5:7b"
    openai_model: str = "gpt-4o"

    # SQL Server (the dedicated ColdChainDB database, never master).
    sql_server_host: str = "localhost"
    sql_server_port: int = 1433
    sql_database: str = "ColdChainDB"
    # One login per purpose (db/roles.sql), including one per agent role.
    sql_agent_user: str = "ccx_reader"  # read-only explainer
    sql_agent_password: SecretStr | None = None
    sql_ingest_user: str = "ccx_ingest"  # ingestion + data-quality runner
    sql_ingest_password: SecretStr | None = None
    sql_console_user: str = "ccx_console"  # dispatcher console, depot row-level security
    sql_console_password: SecretStr | None = None
    sql_monitor_user: str = "ccx_monitor"  # rule hits, incidents, DQ flags
    sql_monitor_password: SecretStr | None = None
    sql_planner_user: str = "ccx_planner"  # drafts only, through a stored procedure
    sql_planner_password: SecretStr | None = None
    sql_gate_user: str = "ccx_gate"  # proposals, approvals, controls, policy versions
    sql_gate_password: SecretStr | None = None
    sql_executor_user: str = "ccx_executor"  # actions; no telemetry
    sql_executor_password: SecretStr | None = None
    # Administrator: used only by `python -m engine.dbadmin init`, never by the app.
    sql_admin_user: str = "sa"
    mssql_sa_password: SecretStr | None = None
    # Accept the self-signed certificate of the local Docker SQL Server. Local use only.
    sql_trust_server_certificate: bool = True

    # SOP retrieval: LOCAL is offline and needs no keys.
    sop_backend: Literal["LOCAL", "PINECONE"] = "LOCAL"

    # Live Open-Meteo adapter. Off by default; tests and replay never use it.
    corridor_live_enabled: bool = False

    # Durable chat memory (LangGraph SQLite checkpointer). Gitignored.
    chat_checkpoint_path: Path = PROJECT_ROOT / "data" / "state" / "chat_checkpoints.sqlite"

    # Tokens all LLM roles may spend in one run; the same budget for every configuration.
    llm_token_budget_per_run: int = 200_000
    # Sampling seed for the local model (with temperature 0, for repeatable replays).
    llm_seed: int = 0

    @field_validator("agent_llm", "sop_backend", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


def load_settings() -> Settings:
    return Settings()
