-- A separate transaction, after any baseline data has committed.
SELECT pg_create_logical_replication_slot('${PG_SLOT}', 'wal2json');
