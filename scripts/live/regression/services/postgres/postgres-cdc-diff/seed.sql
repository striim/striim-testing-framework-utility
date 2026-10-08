-- scripts/live/regression/postgres-cdc-diff/seed.sql
-- Runs into the qasource schema (source_db: postgres-source route, the default) AFTER the app is
-- RUNNING (when: post_start), so the CDC reader's replication slot captures these inserts.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'alpha'), ('2', 'bravo'), ('3', 'charlie');
