-- scripts/live/regression/services/mssql/mssql-diff/ddl_source.sql
-- Runs as the qasource data user (db: mssql-source), before deploy. Idempotent (SQL
-- Server 2016+ supports DROP TABLE IF EXISTS) so serial re-runs start clean.
DROP TABLE IF EXISTS ${MSSQL_SOURCE_SCHEMA}.${TID}SRC;
CREATE TABLE ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID VARCHAR(20) PRIMARY KEY, MSG VARCHAR(100));
