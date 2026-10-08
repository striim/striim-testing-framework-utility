-- scripts/live/regression/services/vertica/vertica-cdc-diff/seed.sql
-- Rows inserted into the Vertica source once the app is RUNNING (post_start), found by
-- IncrementalBatchReader's poll. Runs as the qasource data user (db: vertica-source). One row
-- per INSERT: Vertica's VALUES takes a single row.
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES (1, 'alpha');
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES (2, 'bravo');
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES (3, 'charlie');
