-- Container-init provisioning (runs once, on first cluster init, via
-- /docker-entrypoint-initdb.d — connected to POSTGRES_DB=sltdb as the postgres
-- superuser). Creates the data roles + schemas a developer expects immediately on
-- `docker compose up` (DBeaver, psql). The live-test harness (PgAdmin.ensure_setup)
-- also creates these idempotently at test-resolve, so the two agree; this file just
-- makes them exist without running a test first.
--
-- qasource = source tables/connections, qatarget = target tables/connections; both
-- LOGIN + REPLICATION (the PostgreSQLReader/logical-decoding role needs REPLICATION),
-- password 'striim'. Source table T and a same-named target T coexist as
-- qasource.T / qatarget.T.
CREATE ROLE qasource LOGIN REPLICATION PASSWORD 'striim';
CREATE ROLE qatarget LOGIN REPLICATION PASSWORD 'striim';
GRANT CONNECT ON DATABASE sltdb TO qasource, qatarget;
CREATE SCHEMA qasource AUTHORIZATION qasource;
CREATE SCHEMA qatarget AUTHORIZATION qatarget;

-- Scope the idle-transaction backstop to disposable data roles in this database.
-- The custom server input is read only during first initialization. An intentional
-- held transaction can use SET LOCAL with its own explicit, bounded budget.
DO $idle_budget$
DECLARE
    budget text := COALESCE(NULLIF(current_setting('slt.idle_transaction_timeout_ms', true), ''), '600000');
    role_name text;
BEGIN
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
