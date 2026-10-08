-- scripts/live/regression/services/vertica/vertica-diff/ddl_source.sql
-- Runs as the qasource data user (db: vertica-source), before deploy. Idempotent so serial
-- re-runs start clean.
DROP TABLE IF EXISTS ${VERTICA_SOURCE_SCHEMA}.${TID}src;
CREATE TABLE ${VERTICA_SOURCE_SCHEMA}.${TID}src (id VARCHAR(20) PRIMARY KEY, msg VARCHAR(100));
