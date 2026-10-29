-- Raw layer: the database, the report schema and the raw telemetry tables.
-- The column names are deliberately terse, in the style of an older telematics system;
-- engine/data/schema.py documents each one and mirrors this script for offline tests
-- (checked by test_schema_drift). Safe to re-run: `python -m engine.dbadmin init`.

IF DB_ID(N'ColdChainDB') IS NULL
    CREATE DATABASE ColdChainDB;
GO

USE ColdChainDB;
GO

IF SCHEMA_ID(N'rpt') IS NULL
    EXEC (N'CREATE SCHEMA rpt');
GO

-- Simulator telemetry, one row per device message. Timestamps are text, as the devices
-- send them; rpt views convert them.
IF OBJECT_ID(N'dbo.RFR_TELEM_LOG', N'U') IS NULL
    CREATE TABLE dbo.RFR_TELEM_LOG (
        EVT_DTTM      VARCHAR(32) NULL,  -- device time 'YYYY-MM-DD HH:MM:SS' (UTC)
        RCPT_DTTM     VARCHAR(32) NULL,  -- receive time, same format
        CNSG_NO       VARCHAR(32) NULL,  -- shipment id
        TRLR_NO       VARCHAR(32) NULL,  -- vehicle id
        HUB_CD        VARCHAR(16) NULL,  -- depot id (row-level security key)
        PROD_CLS      VARCHAR(16) NULL,  -- cargo type code
        PKT_SEQ       INT         NULL,  -- device message sequence number
        GPS_Y         FLOAT       NULL,  -- latitude
        GPS_X         FLOAT       NULL,  -- longitude
        RFR_TMP_C     FLOAT       NULL,  -- cargo temperature, degrees C
        LOAD_QLTY_IX  FLOAT       NULL,  -- cargo condition, 1.0 = perfect
        HZD_TIER      VARCHAR(32) NULL,  -- 'Low Risk' | 'Moderate Risk' | 'High Risk'
        LATE_PCT      FLOAT       NULL,  -- delay probability, 0..1
        PORT_Q_IX     FLOAT       NULL,  -- port congestion 0..10 (NULL if not port-bound)
        LANE_HZD_IX   FLOAT       NULL,  -- route risk index 0..10
        ARR_SLIP_MIN  FLOAT       NULL,  -- ETA delay, minutes
        LEG_STAT      CHAR(1)     NULL,  -- 'E' en route, 'A' arrived
        LOAD_FLG      CHAR(1)     NULL   -- set by the ingestion job
    );
GO

-- One row per (shipment, sequence number): backs up the append-and-dedup ingestion.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_TLM_CNSG_SEQ'
               AND object_id = OBJECT_ID(N'dbo.RFR_TELEM_LOG'))
    CREATE UNIQUE INDEX UX_TLM_CNSG_SEQ ON dbo.RFR_TELEM_LOG (CNSG_NO, PKT_SEQ)
        WHERE CNSG_NO IS NOT NULL;
GO

-- Optional Kaggle stress set (python -m engine.data.kaggle). Same shape, kept apart.
IF OBJECT_ID(N'dbo.RFR_TELEM_KGL', N'U') IS NULL
    CREATE TABLE dbo.RFR_TELEM_KGL (
        EVT_DTTM      VARCHAR(32) NULL,
        RCPT_DTTM     VARCHAR(32) NULL,
        CNSG_NO       VARCHAR(32) NULL,
        TRLR_NO       VARCHAR(32) NULL,
        HUB_CD        VARCHAR(16) NULL,
        PROD_CLS      VARCHAR(16) NULL,
        PKT_SEQ       INT         NULL,
        GPS_Y         FLOAT       NULL,
        GPS_X         FLOAT       NULL,
        RFR_TMP_C     FLOAT       NULL,
        LOAD_QLTY_IX  FLOAT       NULL,
        HZD_TIER      VARCHAR(32) NULL,
        LATE_PCT      FLOAT       NULL,
        PORT_Q_IX     FLOAT       NULL,
        LANE_HZD_IX   FLOAT       NULL,
        ARR_SLIP_MIN  FLOAT       NULL,
        LEG_STAT      CHAR(1)     NULL,
        LOAD_FLG      CHAR(1)     NULL
    );
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_KGL_CNSG_SEQ'
               AND object_id = OBJECT_ID(N'dbo.RFR_TELEM_KGL'))
    CREATE UNIQUE INDEX UX_KGL_CNSG_SEQ ON dbo.RFR_TELEM_KGL (CNSG_NO, PKT_SEQ)
        WHERE CNSG_NO IS NOT NULL;
GO
