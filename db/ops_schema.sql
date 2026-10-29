-- Operational schema and the latest-valid-reading view. Safe to re-run.
-- Phase 1: data-quality results. Phase 2: rule hits and incidents. Phase 3: proposals,
-- approvals, actions, audit log and control flags. Phase 4: LLM calls and the event queue.
-- Python mirror for offline tests: engine/data/schema.py.

USE ColdChainDB;
GO

IF SCHEMA_ID(N'ops') IS NULL
    EXEC (N'CREATE SCHEMA ops');
GO

-- Overall data-quality status of every assessed reading.
IF OBJECT_ID(N'ops.ReadingQuality', N'U') IS NULL
    CREATE TABLE ops.ReadingQuality (
        ShipmentID VARCHAR(32) NOT NULL,
        MsgSeqNo   INT         NOT NULL,
        DqStatus   VARCHAR(8)  NOT NULL
            CONSTRAINT CK_ReadingQuality_Status CHECK (DqStatus IN ('valid', 'suspect', 'invalid')),
        AssessedAt DATETIME2(0) NOT NULL,
        CONSTRAINT PK_ReadingQuality PRIMARY KEY (ShipmentID, MsgSeqNo)
    );
GO

-- One row per failed check. MsgSeqNo is NULL for shipment-level flags (freshness).
IF OBJECT_ID(N'ops.DataQualityFlags', N'U') IS NULL
    CREATE TABLE ops.DataQualityFlags (
        FlagID      BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_DataQualityFlags PRIMARY KEY,
        ShipmentID  VARCHAR(32)   NOT NULL,
        MsgSeqNo    INT           NULL,
        CheckName   VARCHAR(32)   NOT NULL,
        DqStatus    VARCHAR(8)    NOT NULL,
        Detail      NVARCHAR(400) NOT NULL,
        EvaluatedAt DATETIME2(0)  NOT NULL
    );
GO

-- Phase 2: every SOP rule hit (on valid and suspect readings). Append-only.
IF OBJECT_ID(N'ops.RuleHits', N'U') IS NULL
    CREATE TABLE ops.RuleHits (
        RuleHitID   BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_RuleHits PRIMARY KEY,
        RuleId      VARCHAR(16)   NOT NULL,
        ShipmentID  VARCHAR(32)   NOT NULL,
        MsgSeqNo    INT           NOT NULL,
        ReadingTs   DATETIME2(0)  NULL,
        DqStatus    VARCHAR(8)    NOT NULL,
        SopRef      VARCHAR(80)   NOT NULL,
        Observed    NVARCHAR(400) NOT NULL,  -- JSON
        Threshold   NVARCHAR(400) NOT NULL,  -- JSON
        EvaluatedAt DATETIME2(0)  NOT NULL
    );
GO

-- Phase 2: incidents opened, merged and cleared by engine/incidents/manager.py.
IF OBJECT_ID(N'ops.Incidents', N'U') IS NULL
    CREATE TABLE ops.Incidents (
        IncidentID VARCHAR(48)  NOT NULL CONSTRAINT PK_Incidents PRIMARY KEY,
        ShipmentID VARCHAR(32)  NOT NULL,
        DepotID    VARCHAR(16)  NULL,
        CargoType  VARCHAR(16)  NULL,
        Status     VARCHAR(16)  NOT NULL
            CONSTRAINT CK_Incidents_Status
            CHECK (Status IN ('open', 'cleared', 'needs_attention')),
        OpenedAt   DATETIME2(0) NOT NULL,
        LastHitAt  DATETIME2(0) NOT NULL,
        ClearedAt  DATETIME2(0) NULL,
        RuleIds    VARCHAR(100) NOT NULL,
        HitCount   INT          NOT NULL,
        FirstSeqNo INT          NOT NULL,
        LastSeqNo  INT          NOT NULL,
        RiskScore  FLOAT        NOT NULL,
        UpdatedAt  DATETIME2(0) NOT NULL
    );
GO

-- At most one open incident per shipment, enforced by the database as well as the code.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_Incidents_OpenPerShipment'
               AND object_id = OBJECT_ID(N'ops.Incidents'))
    CREATE UNIQUE INDEX UX_Incidents_OpenPerShipment ON ops.Incidents (ShipmentID)
        WHERE Status = 'open';
GO

-- Phase 3: proposals from the planner and their gate status.
IF OBJECT_ID(N'ops.Proposals', N'U') IS NULL
    CREATE TABLE ops.Proposals (
        ProposalID   VARCHAR(160)  NOT NULL CONSTRAINT PK_Proposals PRIMARY KEY,
        ProposalHash CHAR(64)      NOT NULL,
        IncidentID   VARCHAR(48)   NOT NULL,
        ShipmentID   VARCHAR(32)   NOT NULL,
        DepotID      VARCHAR(16)   NULL,
        Action       VARCHAR(48)   NOT NULL,
        Target       VARCHAR(48)   NOT NULL,
        Level        CHAR(2)       NOT NULL,
        Status       VARCHAR(24)   NOT NULL
            CONSTRAINT CK_Proposals_Status
            CHECK (Status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'dry_run')),
        Payload      NVARCHAR(MAX) NOT NULL,
        CreatedAt    DATETIME2(0)  NOT NULL,
        UpdatedAt    DATETIME2(0)  NOT NULL
    );
GO

-- Phase 3: human decisions. One per proposal; bound to the exact proposal hash.
IF OBJECT_ID(N'ops.Approvals', N'U') IS NULL
    CREATE TABLE ops.Approvals (
        ApprovalID   VARCHAR(64)   NOT NULL CONSTRAINT PK_Approvals PRIMARY KEY,
        ProposalID   VARCHAR(160)  NOT NULL CONSTRAINT UQ_Approvals_Proposal UNIQUE,
        ProposalHash CHAR(64)      NOT NULL,
        ApproverID   VARCHAR(64)   NOT NULL,
        ApproverRole VARCHAR(32)   NOT NULL,
        Decision     VARCHAR(8)    NOT NULL,
        Reason       NVARCHAR(400) NOT NULL,
        DecidedAt    DATETIME2(0)  NOT NULL,
        RecordedAt   DATETIME2(0)  NOT NULL
    );
GO

-- Phase 3: executed actions. The unique idempotency key (incident:action:target) means the
-- same incident can never trigger the same action on the same target twice.
IF OBJECT_ID(N'ops.Actions', N'U') IS NULL
    CREATE TABLE ops.Actions (
        ActionID       VARCHAR(64)   NOT NULL CONSTRAINT PK_Actions PRIMARY KEY,
        IdempotencyKey VARCHAR(160)  NOT NULL CONSTRAINT UQ_Actions_IdempotencyKey UNIQUE,
        ProposalID     VARCHAR(160)  NOT NULL,
        ProposalHash   CHAR(64)      NOT NULL,
        IncidentID     VARCHAR(48)   NOT NULL,
        ShipmentID     VARCHAR(32)   NOT NULL,
        Action         VARCHAR(48)   NOT NULL,
        Target         VARCHAR(48)   NOT NULL,
        Level          CHAR(2)       NOT NULL,
        Status         VARCHAR(16)   NOT NULL
            CONSTRAINT CK_Actions_Status
            CHECK (Status IN ('queued', 'executing', 'succeeded', 'failed', 'cancelled')),
        Attempts       INT           NOT NULL,
        NextAttemptAt  DATETIME2(0)  NULL,
        LastError      NVARCHAR(400) NULL,
        Result         NVARCHAR(MAX) NOT NULL,
        CreatedAt      DATETIME2(0)  NOT NULL,
        UpdatedAt      DATETIME2(0)  NOT NULL
    );
GO

-- Phase 3: append-only, hash-chained audit log (engine/audit/log.py).
IF OBJECT_ID(N'ops.AuditLog', N'U') IS NULL
    CREATE TABLE ops.AuditLog (
        AuditID    BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_AuditLog PRIMARY KEY,
        At         DATETIME2(0)  NOT NULL,
        Actor      VARCHAR(64)   NOT NULL,
        EventType  VARCHAR(48)   NOT NULL,
        ProposalID VARCHAR(160)  NULL,
        ActionID   VARCHAR(64)   NULL,
        Detail     NVARCHAR(MAX) NOT NULL,
        PrevHash   CHAR(64)      NOT NULL,
        RowHash    CHAR(64)      NOT NULL
    );
GO

-- Phase 3: kill switch and dry run set by a supervisor (can only switch them on over the YAML).
IF OBJECT_ID(N'ops.ControlFlags', N'U') IS NULL
    CREATE TABLE ops.ControlFlags (
        Name      VARCHAR(32)  NOT NULL CONSTRAINT PK_ControlFlags PRIMARY KEY,
        Value     INT          NOT NULL,
        UpdatedBy VARCHAR(64)  NOT NULL,
        UpdatedAt DATETIME2(0) NOT NULL
    );
GO

-- Phase 4: every LLM call (refused and failed ones too), with tokens, latency and text.
IF OBJECT_ID(N'ops.LLMCalls', N'U') IS NULL
    CREATE TABLE ops.LLMCalls (
        CallID           BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_LLMCalls PRIMARY KEY,
        RunID            VARCHAR(64)   NOT NULL,
        CorrelationID    VARCHAR(80)   NOT NULL,
        Role             VARCHAR(32)   NOT NULL,
        Model            VARCHAR(80)   NOT NULL,
        ModelVersion     VARCHAR(80)   NULL,
        PromptTokens     INT           NOT NULL,
        CompletionTokens INT           NOT NULL,
        TokensEstimated  INT           NOT NULL,
        LatencyMs        FLOAT         NOT NULL,
        Ok               INT           NOT NULL,
        Error            VARCHAR(200)  NULL,
        Prompt           NVARCHAR(MAX) NOT NULL,
        Completion       NVARCHAR(MAX) NOT NULL,
        At               DATETIME2(0)  NOT NULL
    );
GO

-- Phase 4: typed messages between agents (engine/bus). Each carries a correlation id.
IF OBJECT_ID(N'ops.EventQueue', N'U') IS NULL
    CREATE TABLE ops.EventQueue (
        MessageID     BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_EventQueue PRIMARY KEY,
        CorrelationID VARCHAR(80)   NOT NULL,
        MessageType   VARCHAR(32)   NOT NULL,
        Producer      VARCHAR(32)   NOT NULL,
        Payload       NVARCHAR(MAX) NOT NULL,
        CreatedAt     DATETIME2(0)  NOT NULL,
        ConsumedBy    VARCHAR(32)   NULL,
        ConsumedAt    DATETIME2(0)  NULL
    );
GO

-- Phase 6: drafts from the planning MCP server. Upgrade the Phase 3 check constraint in place.
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_Proposals_Status'
           AND definition NOT LIKE N'%draft%')
    ALTER TABLE ops.Proposals DROP CONSTRAINT CK_Proposals_Status;
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_Proposals_Status')
    ALTER TABLE ops.Proposals ADD CONSTRAINT CK_Proposals_Status
        CHECK (Status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'dry_run'));
GO

-- Phase 6: the only way the planner login can write a proposal. Status is always 'draft';
-- only the gate login can change it (spec section 10: the planner cannot approve or execute).
CREATE OR ALTER PROCEDURE ops.usp_InsertDraftProposal
    @ProposalID VARCHAR(160), @ProposalHash CHAR(64), @IncidentID VARCHAR(48),
    @ShipmentID VARCHAR(32), @DepotID VARCHAR(16), @Action VARCHAR(48), @Target VARCHAR(48),
    @Level CHAR(2), @Payload NVARCHAR(MAX), @At DATETIME2(0)
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO ops.Proposals
        (ProposalID, ProposalHash, IncidentID, ShipmentID, DepotID, Action, Target, Level,
         Status, Payload, CreatedAt, UpdatedAt)
    VALUES
        (@ProposalID, @ProposalHash, @IncidentID, @ShipmentID, @DepotID, @Action, @Target, @Level,
         'draft', @Payload, @At, @At);
END;
GO

-- Phase 6: every accepted action-catalogue version (PUT /admin/policy).
IF OBJECT_ID(N'ops.PolicyVersions', N'U') IS NULL
    CREATE TABLE ops.PolicyVersions (
        Version    INT           NOT NULL CONSTRAINT PK_PolicyVersions PRIMARY KEY,
        PolicyHash CHAR(64)      NOT NULL,
        YamlText   NVARCHAR(MAX) NOT NULL,
        Author     VARCHAR(64)   NOT NULL,
        Reason     NVARCHAR(400) NOT NULL,
        CreatedAt  DATETIME2(0)  NOT NULL
    );
GO

-- Phase 3: incidents can be marked needs_attention when an action fails after its retries.
-- Upgrade the Phase 2 check constraint in place.
IF EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_Incidents_Status'
           AND definition NOT LIKE N'%needs_attention%')
    ALTER TABLE ops.Incidents DROP CONSTRAINT CK_Incidents_Status;
IF NOT EXISTS (SELECT 1 FROM sys.check_constraints WHERE name = N'CK_Incidents_Status')
    ALTER TABLE ops.Incidents ADD CONSTRAINT CK_Incidents_Status
        CHECK (Status IN ('open', 'cleared', 'needs_attention'));
GO

-- One row per shipment: its latest reading that passed every data-quality check
-- (by device time, then sequence). Rows a session may see are limited by the depot
-- row-level security policy on the raw table (db/roles.sql).
-- Python reference implementation: engine/data/latest.py.
CREATE OR ALTER VIEW rpt.VW_LATEST_VALID_READING AS
SELECT
    ShipmentID, VehicleID, DepotID, CargoType, MsgSeqNo, ReadingTs, Latitude, Longitude,
    TemperatureC, CargoConditionCode, RiskClassification, DelayProbability,
    PortCongestionLevel, RouteRiskIndex, EtaDelayMin, TripStatus
FROM (
    SELECT
        r.CNSG_NO                              AS ShipmentID,
        r.TRLR_NO                              AS VehicleID,
        r.HUB_CD                              AS DepotID,
        r.PROD_CLS                          AS CargoType,
        r.PKT_SEQ                          AS MsgSeqNo,
        TRY_CONVERT(DATETIME2(0), r.EVT_DTTM, 120) AS ReadingTs,
        r.GPS_Y                               AS Latitude,
        r.GPS_X                               AS Longitude,
        r.RFR_TMP_C                      AS TemperatureC,
        r.LOAD_QLTY_IX                         AS CargoConditionCode,
        r.HZD_TIER                        AS RiskClassification,
        r.LATE_PCT                      AS DelayProbability,
        r.PORT_Q_IX                         AS PortCongestionLevel,
        r.LANE_HZD_IX                          AS RouteRiskIndex,
        r.ARR_SLIP_MIN                         AS EtaDelayMin,
        r.LEG_STAT                         AS TripStatus,
        ROW_NUMBER() OVER (
            PARTITION BY r.CNSG_NO ORDER BY r.EVT_DTTM DESC, r.PKT_SEQ DESC
        ) AS rn
    FROM dbo.RFR_TELEM_LOG AS r
    JOIN ops.ReadingQuality AS q
        ON q.ShipmentID = r.CNSG_NO AND q.MsgSeqNo = r.PKT_SEQ
    WHERE q.DqStatus = 'valid'
) AS ranked
WHERE rn = 1;
GO
