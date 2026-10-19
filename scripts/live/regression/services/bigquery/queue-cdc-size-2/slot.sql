-- scripts/live/regression/postgres-cdc-diff/slot.sql
-- Creates the wal2json logical replication slot the PostgreSQLReader attaches to
-- (Striim does not auto-create it). Kept in its OWN ddl file so it runs on a fresh
-- connection with no prior writes — pg_create_logical_replication_slot cannot run
-- in a transaction that has already performed writes (e.g. the CREATE TABLEs).
-- Idempotent for serial re-runs; slot name = a per-test-unique identifier (schema_for(test),
-- unrelated to the fixed qasource/qatarget schemas; matches the app's ReplicationSlotName).
-- The slot functions are global (search_path does not matter).
SELECT pg_drop_replication_slot('${PG_SLOT}') FROM pg_replication_slots WHERE slot_name = '${PG_SLOT}';
SELECT pg_create_logical_replication_slot('${PG_SLOT}', 'wal2json');
