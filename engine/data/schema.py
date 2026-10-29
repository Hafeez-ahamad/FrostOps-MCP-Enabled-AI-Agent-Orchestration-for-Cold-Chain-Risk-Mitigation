"""SQLAlchemy table definitions used by Python code.

SQL Server objects are created by the scripts in db/ (the source of truth there).
These definitions mirror them so the same queries run against SQLite in offline tests;
`tests/integration/test_schema_drift.py` checks that both agree.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

# Legacy raw telemetry. Cryptic column names are part of the legacy-integration story.
RAW_COLUMNS = [
    "EVT_DTTM",  # device timestamp, text 'YYYY-MM-DD HH:MM:SS' (UTC)
    "RCPT_DTTM",  # time the message was received, same format
    "CNSG_NO",  # shipment id
    "TRLR_NO",  # vehicle id
    "HUB_CD",  # depot id (row-level security key)
    "PROD_CLS",  # cargo type code (policy/cargo_types.yaml)
    "PKT_SEQ",  # device message sequence number, per shipment
    "GPS_Y",
    "GPS_X",
    "RFR_TMP_C",  # cargo temperature, degrees C
    "LOAD_QLTY_IX",  # cargo condition, 1.0 = perfect
    "HZD_TIER",  # 'Low Risk' | 'Moderate Risk' | 'High Risk'
    "LATE_PCT",  # delay probability 0..1
    "PORT_Q_IX",  # port congestion 0..10 (NULL when the shipment is not port-bound)
    "LANE_HZD_IX",  # route risk index 0..10
    "ARR_SLIP_MIN",  # current ETA delay, minutes
    "LEG_STAT",  # 'E' en route, 'A' arrived (last reading of a trip)
    "LOAD_FLG",
]


def _raw_table(name: str) -> Table:
    return Table(
        name,
        metadata,
        Column("EVT_DTTM", String(32)),
        Column("RCPT_DTTM", String(32)),
        Column("CNSG_NO", String(32)),
        Column("TRLR_NO", String(32)),
        Column("HUB_CD", String(16)),
        Column("PROD_CLS", String(16)),
        Column("PKT_SEQ", Integer),
        Column("GPS_Y", Float),
        Column("GPS_X", Float),
        Column("RFR_TMP_C", Float),
        Column("LOAD_QLTY_IX", Float),
        Column("HZD_TIER", String(32)),
        Column("LATE_PCT", Float),
        Column("PORT_Q_IX", Float),
        Column("LANE_HZD_IX", Float),
        Column("ARR_SLIP_MIN", Float),
        Column("LEG_STAT", String(1)),
        Column("LOAD_FLG", String(1)),
        schema="dbo",
    )


RAW_TABLE = _raw_table("RFR_TELEM_LOG")
# Optional Kaggle stress set: same shape, separate table so it never mixes with the simulator.
KAGGLE_TABLE = _raw_table("RFR_TELEM_KGL")

DQ_STATUSES = ("valid", "suspect", "invalid")

READING_QUALITY = Table(
    "ReadingQuality",
    metadata,
    Column("ShipmentID", String(32), primary_key=True),
    Column("MsgSeqNo", Integer, primary_key=True),
    Column("DqStatus", String(8), nullable=False),
    Column("AssessedAt", DateTime, nullable=False),
    CheckConstraint("DqStatus IN ('valid', 'suspect', 'invalid')", name="CK_ReadingQuality_Status"),
    schema="ops",
)

DATA_QUALITY_FLAGS = Table(
    "DataQualityFlags",
    metadata,
    Column("FlagID", Integer, primary_key=True, autoincrement=True),
    Column("ShipmentID", String(32), nullable=False),
    Column("MsgSeqNo", Integer, nullable=True),  # NULL for shipment-level flags (freshness)
    Column("CheckName", String(32), nullable=False),
    Column("DqStatus", String(8), nullable=False),
    Column("Detail", String(400), nullable=False),
    Column("EvaluatedAt", DateTime, nullable=False),
    schema="ops",
)

# Phase 2: every rule hit (valid and suspect readings) and the incidents built from them.
RULE_HITS = Table(
    "RuleHits",
    metadata,
    Column("RuleHitID", Integer, primary_key=True, autoincrement=True),
    Column("RuleId", String(16), nullable=False),
    Column("ShipmentID", String(32), nullable=False),
    Column("MsgSeqNo", Integer, nullable=False),
    Column("ReadingTs", DateTime, nullable=True),
    Column("DqStatus", String(8), nullable=False),
    Column("SopRef", String(80), nullable=False),
    Column("Observed", String(400), nullable=False),  # JSON
    Column("Threshold", String(400), nullable=False),  # JSON
    Column("EvaluatedAt", DateTime, nullable=False),
    schema="ops",
)

INCIDENTS = Table(
    "Incidents",
    metadata,
    Column("IncidentID", String(48), primary_key=True),
    Column("ShipmentID", String(32), nullable=False),
    Column("DepotID", String(16), nullable=True),
    Column("CargoType", String(16), nullable=True),
    Column("Status", String(16), nullable=False),
    Column("OpenedAt", DateTime, nullable=False),
    Column("LastHitAt", DateTime, nullable=False),
    Column("ClearedAt", DateTime, nullable=True),
    Column("RuleIds", String(100), nullable=False),  # comma-separated, first-seen order
    Column("HitCount", Integer, nullable=False),
    Column("FirstSeqNo", Integer, nullable=False),
    Column("LastSeqNo", Integer, nullable=False),
    Column("RiskScore", Float, nullable=False),
    Column("UpdatedAt", DateTime, nullable=False),
    CheckConstraint("Status IN ('open', 'cleared', 'needs_attention')", name="CK_Incidents_Status"),
    schema="ops",
)

# Phase 3: proposals, human approvals, executed actions, the audit log and control flags.
PROPOSALS = Table(
    "Proposals",
    metadata,
    Column("ProposalID", String(160), primary_key=True),
    Column("ProposalHash", String(64), nullable=False),
    Column("IncidentID", String(48), nullable=False),
    Column("ShipmentID", String(32), nullable=False),
    Column("DepotID", String(16), nullable=True),
    Column("Action", String(48), nullable=False),
    Column("Target", String(48), nullable=False),
    Column("Level", String(2), nullable=False),
    Column("Status", String(24), nullable=False),
    Column("Payload", Text, nullable=False),  # the full proposal as JSON
    Column("CreatedAt", DateTime, nullable=False),
    Column("UpdatedAt", DateTime, nullable=False),
    CheckConstraint(
        "Status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'dry_run')",
        name="CK_Proposals_Status",
    ),
    schema="ops",
)

APPROVALS = Table(
    "Approvals",
    metadata,
    Column("ApprovalID", String(64), primary_key=True),
    Column("ProposalID", String(160), nullable=False, unique=True),  # one human decision each
    Column("ProposalHash", String(64), nullable=False),
    Column("ApproverID", String(64), nullable=False),
    Column("ApproverRole", String(32), nullable=False),
    Column("Decision", String(8), nullable=False),
    Column("Reason", String(400), nullable=False),
    Column("DecidedAt", DateTime, nullable=False),
    Column("RecordedAt", DateTime, nullable=False),
    schema="ops",
)

ACTIONS = Table(
    "Actions",
    metadata,
    Column("ActionID", String(64), primary_key=True),
    Column("IdempotencyKey", String(160), nullable=False, unique=True),
    Column("ProposalID", String(160), nullable=False),
    Column("ProposalHash", String(64), nullable=False),
    Column("IncidentID", String(48), nullable=False),
    Column("ShipmentID", String(32), nullable=False),
    Column("Action", String(48), nullable=False),
    Column("Target", String(48), nullable=False),
    Column("Level", String(2), nullable=False),
    Column("Status", String(16), nullable=False),
    Column("Attempts", Integer, nullable=False),
    Column("NextAttemptAt", DateTime, nullable=True),
    Column("LastError", String(400), nullable=True),
    Column("Result", Text, nullable=False),  # JSON
    Column("CreatedAt", DateTime, nullable=False),
    Column("UpdatedAt", DateTime, nullable=False),
    CheckConstraint(
        "Status IN ('queued', 'executing', 'succeeded', 'failed', 'cancelled')",
        name="CK_Actions_Status",
    ),
    schema="ops",
)

AUDIT_LOG = Table(
    "AuditLog",
    metadata,
    Column("AuditID", Integer, primary_key=True, autoincrement=True),
    Column("At", DateTime, nullable=False),
    Column("Actor", String(64), nullable=False),
    Column("EventType", String(48), nullable=False),
    Column("ProposalID", String(160), nullable=True),
    Column("ActionID", String(64), nullable=True),
    Column("Detail", Text, nullable=False),  # JSON
    Column("PrevHash", String(64), nullable=False),
    Column("RowHash", String(64), nullable=False),
    schema="ops",
)

CONTROL_FLAGS = Table(
    "ControlFlags",
    metadata,
    Column("Name", String(32), primary_key=True),  # 'kill_switch' | 'dry_run'
    Column("Value", Integer, nullable=False),  # 0 / 1
    Column("UpdatedBy", String(64), nullable=False),
    Column("UpdatedAt", DateTime, nullable=False),
    schema="ops",
)

# Phase 6: every accepted action-catalogue version (PUT /admin/policy).
POLICY_VERSIONS = Table(
    "PolicyVersions",
    metadata,
    Column("Version", Integer, primary_key=True, autoincrement=False),
    Column("PolicyHash", String(64), nullable=False),
    Column("YamlText", Text, nullable=False),
    Column("Author", String(64), nullable=False),
    Column("Reason", String(400), nullable=False),
    Column("CreatedAt", DateTime, nullable=False),
    schema="ops",
)

# Phase 4: every LLM call, and the typed message queue between agents.
LLM_CALLS = Table(
    "LLMCalls",
    metadata,
    Column("CallID", Integer, primary_key=True, autoincrement=True),
    Column("RunID", String(64), nullable=False),
    Column("CorrelationID", String(80), nullable=False),
    Column("Role", String(32), nullable=False),
    Column("Model", String(80), nullable=False),
    Column("ModelVersion", String(80), nullable=True),
    Column("PromptTokens", Integer, nullable=False),
    Column("CompletionTokens", Integer, nullable=False),
    Column("TokensEstimated", Integer, nullable=False),  # 1 if the model reported no usage
    Column("LatencyMs", Float, nullable=False),
    Column("Ok", Integer, nullable=False),
    Column("Error", String(200), nullable=True),
    Column("Prompt", Text, nullable=False),
    Column("Completion", Text, nullable=False),
    Column("At", DateTime, nullable=False),
    schema="ops",
)

EVENT_QUEUE = Table(
    "EventQueue",
    metadata,
    Column("MessageID", Integer, primary_key=True, autoincrement=True),
    Column("CorrelationID", String(80), nullable=False),
    Column("MessageType", String(32), nullable=False),
    Column("Producer", String(32), nullable=False),
    Column("Payload", Text, nullable=False),  # JSON, validated by engine/bus/messages.py
    Column("CreatedAt", DateTime, nullable=False),
    Column("ConsumedBy", String(32), nullable=True),
    Column("ConsumedAt", DateTime, nullable=True),
    schema="ops",
)

# The view rpt.VW_LATEST_VALID_READING (db/ops_schema.sql): one row per shipment.
# Declared as a Table so queries can be built with SQLAlchemy; tests create it as a table.
LATEST_VALID_READING = Table(
    "VW_LATEST_VALID_READING",
    MetaData(),
    Column("ShipmentID", String(32)),
    Column("VehicleID", String(32)),
    Column("DepotID", String(16)),
    Column("CargoType", String(16)),
    Column("MsgSeqNo", Integer),
    Column("ReadingTs", DateTime),
    Column("Latitude", Float),
    Column("Longitude", Float),
    Column("TemperatureC", Float),
    Column("CargoConditionCode", Float),
    Column("RiskClassification", String(32)),
    Column("DelayProbability", Float),
    Column("PortCongestionLevel", Float),
    Column("RouteRiskIndex", Float),
    Column("EtaDelayMin", Float),
    Column("TripStatus", String(1)),
    schema="rpt",
)
