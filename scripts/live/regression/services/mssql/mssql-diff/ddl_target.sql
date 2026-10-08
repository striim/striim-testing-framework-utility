-- scripts/live/regression/services/mssql/mssql-diff/ddl_target.sql
-- Runs as the qatarget data user (db: mssql-target), before deploy. Idempotent.
DROP TABLE IF EXISTS ${MSSQL_TARGET_SCHEMA}.${TID}TGT;
CREATE TABLE ${MSSQL_TARGET_SCHEMA}.${TID}TGT (ID VARCHAR(20) PRIMARY KEY, MSG VARCHAR(100));
