-- Keep slot creation in its own DDL file: PostgreSQL executes one multi-statement
-- query in one implicit transaction, and a slot cannot be created after a write.
-- The lifecycle ownership ledger refuses collisions and drops this per-test slot.
SELECT pg_create_logical_replication_slot('${PG_SLOT}', 'wal2json');
