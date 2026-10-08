-- scripts/live/regression/services/mssql/mssql-cdc-diff/ddl_source.sql
-- Runs as the qasource data user (db: mssql-source), before deploy. Idempotent: a prior
-- run leaves ${MSSQL_SOURCE_SCHEMA}.${TID}SRC CDC-tracked (the capture instance survives a plain DROP TABLE),
-- so disable CDC on it first, then drop+recreate, then re-enable table-level CDC so the
-- SQL Server Agent capture job records inserts for MSSqlReader. qasource is a db_owner
-- member (see MssqlAdmin.ensure_setup), which is sufficient to call sp_cdc_enable_table/
-- sp_cdc_disable_table on its own schema without needing the sa login.
IF EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id
           WHERE s.name = '${MSSQL_SOURCE_SCHEMA}' AND t.name = '${TID}SRC' AND t.is_tracked_by_cdc = 1)
    EXEC sys.sp_cdc_disable_table @source_schema = N'${MSSQL_SOURCE_SCHEMA}', @source_name = N'${TID}SRC', @capture_instance = N'all';
DROP TABLE IF EXISTS ${MSSQL_SOURCE_SCHEMA}.${TID}SRC;
CREATE TABLE ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID VARCHAR(20) PRIMARY KEY, MSG VARCHAR(100));
EXEC sys.sp_cdc_enable_table @source_schema = N'${MSSQL_SOURCE_SCHEMA}', @source_name = N'${TID}SRC', @role_name = NULL, @supports_net_changes = 0
