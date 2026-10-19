-- Postgres role/schema setup for the integration test tier.
--
-- This is the canonical copy mounted directly into /docker-entrypoint-initdb.d
-- by compose.yaml, and can also be applied with a plain SQL client.
--
-- Mirrors scripts/live/services/postgres/init-qausers.sql (the live-tier
-- container-init script) but simplified for integration use: no REPLICATION
-- grant, since these tests hit tables directly (DatabaseReader/Writer-style
-- access) rather than logical-decoding CDC. If a future integration test
-- needs PostgreSQLReader/CDC against this database, add REPLICATION to the
-- roles below (see the live-tier file for the pattern).
--
-- qasource = source-side tables, qatarget = target-side tables; both LOGIN,
-- password 'striim'. A source table T and a same-named target table T can
-- coexist as qasource.T / qatarget.T without colliding.
--
-- Run once per test database (idempotent-ish: re-running will error on
-- already-created roles/schemas; DROP/recreate the database between full
-- resets instead of re-running this file against a live one).
CREATE ROLE qasource LOGIN PASSWORD 'striim';
CREATE ROLE qatarget LOGIN PASSWORD 'striim';

-- intdb is the integration tier's database (see services/postgres/compose.yaml
-- POSTGRES_DB / service.yaml docker_defaults.dbname) — distinct from the live
-- tier's sltdb, so both stacks can run at once.
GRANT CONNECT ON DATABASE intdb TO qasource, qatarget;

CREATE SCHEMA qasource AUTHORIZATION qasource;
CREATE SCHEMA qatarget AUTHORIZATION qatarget;

-- Let each role fully manage objects in its own schema (create/alter/drop
-- tables at test-setup and teardown time) without needing superuser.
GRANT ALL ON SCHEMA qasource TO qasource;
GRANT ALL ON SCHEMA qatarget TO qatarget;

-- Optional idle-transaction policy for direct container initialization.
-- Framework bring-up reconciles policy separately, including reused volumes.
-- Unset by default: Striim reader/writer transactions may legitimately stay idle.
DO $idle_budget$
DECLARE
    budget text := NULLIF(current_setting('slt.idle_transaction_timeout_ms', true), '');
    role_name text;
BEGIN
    IF budget IS NULL THEN
        RETURN;
    END IF;
    IF budget !~ '^[1-9][0-9]{0,9}$' THEN
        RAISE EXCEPTION 'invalid-postgres-idle-transaction-budget: expected positive milliseconds';
    END IF;
    IF budget::bigint > 2147483647 THEN
        RAISE EXCEPTION 'invalid-postgres-idle-transaction-budget: exceeds server limit';
    END IF;
    FOREACH role_name IN ARRAY ARRAY['qasource', 'qatarget'] LOOP
        EXECUTE format('ALTER ROLE %I IN DATABASE %I SET idle_in_transaction_session_timeout = %L',
                       role_name, current_database(), budget || 'ms');
    END LOOP;
END;
$idle_budget$;
