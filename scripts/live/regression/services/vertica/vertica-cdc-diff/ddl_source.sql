-- scripts/live/regression/services/vertica/vertica-cdc-diff/ddl_source.sql
-- Runs as the qasource data user (db: vertica-source), before deploy. Idempotent so serial
-- re-runs start clean. id is an INT so it can serve as IncrementalBatchReader's CheckColumn.
DROP TABLE IF EXISTS ${VERTICA_SOURCE_SCHEMA}.${TID}src;
CREATE TABLE ${VERTICA_SOURCE_SCHEMA}.${TID}src (id INT PRIMARY KEY, msg VARCHAR(100));
