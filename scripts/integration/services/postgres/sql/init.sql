-- Postgres role/schema setup for the integration test tier.
--
-- This is the canonical/shared copy of the qasource/qatarget setup — the
-- actual copy wired into Docker at container-init time is
-- services/postgres/init.sql (mounted read-only into
-- /docker-entrypoint-initdb.d/ by services/postgres/compose.yaml and run
-- against POSTGRES_DB=intdb). Keep the two in sync; this one exists under
-- sql/ so the harness can also (re-)apply it directly via a plain client
-- (e.g. psql) against a reused/live Postgres that the framework didn't
-- provision — see services/postgres/service.yaml's live_override_env
-- (INT_PG_HOST) — where there is no docker-entrypoint-initdb.d to mount
-- into.
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
