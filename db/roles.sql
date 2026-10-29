-- Database identities, grants and depot row-level security. Safe to re-run.
-- One login per purpose (spec section 10):
--   ccx_ingest    ingestion and data-quality runner: raw tables + DQ tables (append only)
--   ccx_monitor   monitor: reads the latest-valid view, DQ and rule hits; writes rule hits
--                     and incidents
--   ccx_planner   planner: reads incidents and rule hits; writes proposals only as drafts,
--                     through ops.usp_InsertDraftProposal (no table INSERT, no UPDATE)
--   ccx_gate      policy gate service: the only login that changes a proposal's status;
--                     records approvals, control flags and policy versions
--   ccx_executor  executor service: reads approved proposals and approvals; writes Actions;
--                     cannot read any telemetry
--   ccx_reader        explainer: read-only (view, incidents, proposals, actions, audit)
--   ccx_console   dispatcher console: latest-valid view only, limited to the depots the API
--                     puts in SESSION_CONTEXT('depot_ids') for the signed-in dispatcher
-- Every agent and service login can INSERT into ops.AuditLog (and read the last hash to chain
-- it), but nobody can UPDATE or DELETE it. Everyone is denied the raw table except ingestion.
--
-- Passwords are never written here. engine/dbadmin.py substitutes the *_PASSWORD variables
-- from .env at run time.

USE ColdChainDB;
GO

-- Logins and users -----------------------------------------------------------------------
IF SUSER_ID(N'ccx_reader') IS NULL
    CREATE LOGIN ccx_reader WITH PASSWORD = N'$(AGENT_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_reader WITH PASSWORD = N'$(AGENT_PASSWORD)';
IF SUSER_ID(N'ccx_ingest') IS NULL
    CREATE LOGIN ccx_ingest WITH PASSWORD = N'$(INGEST_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_ingest WITH PASSWORD = N'$(INGEST_PASSWORD)';
IF SUSER_ID(N'ccx_console') IS NULL
    CREATE LOGIN ccx_console WITH PASSWORD = N'$(CONSOLE_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_console WITH PASSWORD = N'$(CONSOLE_PASSWORD)';
IF SUSER_ID(N'ccx_monitor') IS NULL
    CREATE LOGIN ccx_monitor WITH PASSWORD = N'$(MONITOR_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_monitor WITH PASSWORD = N'$(MONITOR_PASSWORD)';
IF SUSER_ID(N'ccx_planner') IS NULL
    CREATE LOGIN ccx_planner WITH PASSWORD = N'$(PLANNER_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_planner WITH PASSWORD = N'$(PLANNER_PASSWORD)';
IF SUSER_ID(N'ccx_gate') IS NULL
    CREATE LOGIN ccx_gate WITH PASSWORD = N'$(GATE_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_gate WITH PASSWORD = N'$(GATE_PASSWORD)';
IF SUSER_ID(N'ccx_executor') IS NULL
    CREATE LOGIN ccx_executor WITH PASSWORD = N'$(EXECUTOR_PASSWORD)', CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ccx_executor WITH PASSWORD = N'$(EXECUTOR_PASSWORD)';
GO

IF USER_ID(N'ccx_reader') IS NULL CREATE USER ccx_reader FOR LOGIN ccx_reader;
IF USER_ID(N'ccx_ingest') IS NULL CREATE USER ccx_ingest FOR LOGIN ccx_ingest;
IF USER_ID(N'ccx_console') IS NULL CREATE USER ccx_console FOR LOGIN ccx_console;
IF USER_ID(N'ccx_monitor') IS NULL CREATE USER ccx_monitor FOR LOGIN ccx_monitor;
IF USER_ID(N'ccx_planner') IS NULL CREATE USER ccx_planner FOR LOGIN ccx_planner;
IF USER_ID(N'ccx_gate') IS NULL CREATE USER ccx_gate FOR LOGIN ccx_gate;
IF USER_ID(N'ccx_executor') IS NULL CREATE USER ccx_executor FOR LOGIN ccx_executor;
GO

-- Members of this role see every depot (service accounts, not people).
IF DATABASE_PRINCIPAL_ID(N'rl_all_depots') IS NULL CREATE ROLE rl_all_depots;
GO
IF IS_ROLEMEMBER(N'rl_all_depots', N'ccx_reader') = 0
    ALTER ROLE rl_all_depots ADD MEMBER ccx_reader;
IF IS_ROLEMEMBER(N'rl_all_depots', N'ccx_ingest') = 0
    ALTER ROLE rl_all_depots ADD MEMBER ccx_ingest;
IF IS_ROLEMEMBER(N'rl_all_depots', N'ccx_monitor') = 0
    ALTER ROLE rl_all_depots ADD MEMBER ccx_monitor;
GO

-- Grants (readers get the view, the raw table is denied) ---------------------------
-- The view and the tables share an owner (dbo), so ownership chaining lets a login read the
-- view while direct reads of the raw table stay denied.
GRANT SELECT ON rpt.VW_LATEST_VALID_READING TO ccx_reader, ccx_console;
DENY SELECT ON dbo.RFR_TELEM_LOG TO ccx_reader, ccx_console;
DENY SELECT ON dbo.RFR_TELEM_KGL TO ccx_reader, ccx_console;
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::dbo TO ccx_reader, ccx_console;
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::ops TO ccx_reader, ccx_console;

-- Ingestion appends only: no UPDATE or DELETE anywhere.
GRANT SELECT, INSERT ON dbo.RFR_TELEM_LOG TO ccx_ingest;
GRANT SELECT, INSERT ON dbo.RFR_TELEM_KGL TO ccx_ingest;
GRANT SELECT, INSERT ON ops.ReadingQuality TO ccx_ingest;
GRANT SELECT, INSERT ON ops.DataQualityFlags TO ccx_ingest;
-- Phase 2: the detection pipeline runs under the ingest login until Phase 7 gives the monitor
-- its own login (spec section 10: Monitor writes Incidents).
GRANT SELECT, INSERT ON ops.RuleHits TO ccx_ingest;
GRANT SELECT, INSERT, UPDATE ON ops.Incidents TO ccx_ingest;
DENY UPDATE, DELETE, ALTER ON SCHEMA::dbo TO ccx_ingest;
DENY DELETE, ALTER ON SCHEMA::ops TO ccx_ingest;
GO

-- Phase 7: per-agent grants (spec section 10) ----------------------------------------------------
-- Shared by every agent and service login: append to the audit chain, log LLM calls, use the
-- typed message queue (read and mark consumed). Nobody may change or delete audit rows.
GRANT INSERT ON ops.AuditLog TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
GRANT SELECT (AuditID, RowHash) ON ops.AuditLog
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
GRANT INSERT ON ops.LLMCalls TO ccx_monitor, ccx_planner;
GRANT SELECT, INSERT ON ops.EventQueue TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
GRANT UPDATE (ConsumedBy, ConsumedAt) ON ops.EventQueue
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
DENY UPDATE, DELETE ON ops.AuditLog
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor, ccx_ingest,
       ccx_reader, ccx_console;
DENY SELECT ON dbo.RFR_TELEM_LOG
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
DENY SELECT ON dbo.RFR_TELEM_KGL
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::dbo
    TO ccx_monitor, ccx_planner, ccx_gate, ccx_executor;

-- Monitor: telemetry (through the view), DQ results and rule hits in; rule hits and incidents out.
GRANT SELECT ON rpt.VW_LATEST_VALID_READING TO ccx_monitor;
GRANT SELECT ON ops.ReadingQuality TO ccx_monitor;
GRANT SELECT, INSERT ON ops.DataQualityFlags TO ccx_monitor;  -- freshness flags
GRANT SELECT, INSERT ON ops.RuleHits TO ccx_monitor;
GRANT SELECT, INSERT, UPDATE ON ops.Incidents TO ccx_monitor;

-- Planner: incidents and their rule hits in; draft proposals out, only through the procedure.
GRANT SELECT ON ops.Incidents TO ccx_planner;
GRANT SELECT ON ops.RuleHits TO ccx_planner;
GRANT EXECUTE ON ops.usp_InsertDraftProposal TO ccx_planner;
DENY INSERT, UPDATE, DELETE ON ops.Proposals TO ccx_planner;
DENY INSERT, UPDATE, DELETE ON ops.Actions TO ccx_planner;
DENY INSERT, UPDATE, DELETE ON ops.Approvals TO ccx_planner;

-- Gate: decides proposals, records human approvals, controls and policy versions; reads the
-- evidence (rule hits, DQ flags) and recent actions for limits. It never executes.
GRANT SELECT, INSERT, UPDATE ON ops.Proposals TO ccx_gate;
GRANT SELECT, INSERT ON ops.Approvals TO ccx_gate;
GRANT SELECT ON ops.Actions TO ccx_gate;
GRANT SELECT ON ops.RuleHits TO ccx_gate;
GRANT SELECT ON ops.DataQualityFlags TO ccx_gate;
GRANT SELECT, INSERT, UPDATE ON ops.ControlFlags TO ccx_gate;
GRANT SELECT, INSERT ON ops.PolicyVersions TO ccx_gate;
DENY INSERT, UPDATE, DELETE ON ops.Actions TO ccx_gate;

-- Executor: approved proposals, approvals and control flags in; Actions out. No telemetry at all.
GRANT SELECT ON ops.Proposals TO ccx_executor;
GRANT SELECT ON ops.Approvals TO ccx_executor;
GRANT SELECT ON ops.ControlFlags TO ccx_executor;
GRANT SELECT, INSERT, UPDATE ON ops.Actions TO ccx_executor;
DENY SELECT ON rpt.VW_LATEST_VALID_READING TO ccx_executor;
DENY SELECT ON ops.RuleHits TO ccx_executor;
DENY SELECT ON ops.ReadingQuality TO ccx_executor;
DENY SELECT ON ops.DataQualityFlags TO ccx_executor;
DENY INSERT, UPDATE, DELETE ON ops.Proposals TO ccx_executor;
DENY INSERT, UPDATE, DELETE ON ops.Approvals TO ccx_executor;

-- Explainer (ccx_reader): read-only over what it explains; writes nothing (DENY on ops above).
GRANT SELECT ON ops.Incidents TO ccx_reader;
GRANT SELECT ON ops.Proposals TO ccx_reader;
GRANT SELECT ON ops.Actions TO ccx_reader;
GRANT SELECT ON ops.AuditLog TO ccx_reader;
GRANT SELECT ON ops.RuleHits TO ccx_reader;
GO

-- Row-level security by depot -------------------------------------------------------------
IF SCHEMA_ID(N'sec') IS NULL
    EXEC (N'CREATE SCHEMA sec');
GO

-- The function cannot be altered while a policy uses it, so drop the policy first.
IF EXISTS (SELECT 1 FROM sys.security_policies WHERE name = N'DepotFilter')
    DROP SECURITY POLICY sec.DepotFilter;
GO

-- A row is visible to: the database owner (administration), members of rl_all_depots
-- (service accounts), or a session whose SESSION_CONTEXT('depot_ids') lists the row's depot,
-- as '|DEP-LB|DEP-VER|'. A session with no context sees nothing.
CREATE OR ALTER FUNCTION sec.fn_depot_visible(@DepotID VARCHAR(16))
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS visible
    WHERE IS_MEMBER(N'db_owner') = 1
       OR IS_ROLEMEMBER(N'rl_all_depots') = 1
       OR CHARINDEX(
              N'|' + CAST(@DepotID AS NVARCHAR(16)) + N'|',
              CAST(SESSION_CONTEXT(N'depot_ids') AS NVARCHAR(4000))
          ) > 0;
GO

CREATE SECURITY POLICY sec.DepotFilter
    ADD FILTER PREDICATE sec.fn_depot_visible(HUB_CD) ON dbo.RFR_TELEM_LOG
    WITH (STATE = ON);
GO
