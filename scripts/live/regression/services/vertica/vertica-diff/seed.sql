-- scripts/live/regression/services/vertica/vertica-diff/seed.sql
-- Rows in the Vertica source, read by DatabaseReader as an initial load (pre_deploy).
-- Runs as the qasource data user (db: vertica-source). One row per INSERT: Vertica's VALUES
-- takes a single row.
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES ('1', 'alpha');
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES ('2', 'bravo');
INSERT INTO ${VERTICA_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES ('3', 'charlie');
