-- scripts/live/regression/services/vertica/vertica-diff/ddl_target.sql
-- Runs as the qatarget data user (db: vertica-target), before deploy. Idempotent.
DROP TABLE IF EXISTS ${VERTICA_TARGET_SCHEMA}.${TID}tgt;
CREATE TABLE ${VERTICA_TARGET_SCHEMA}.${TID}tgt (id VARCHAR(20) PRIMARY KEY, msg VARCHAR(100));
