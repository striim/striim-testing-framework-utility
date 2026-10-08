-- MySQL user/schema setup for the integration test tier.
--
-- This is the canonical/shared copy of the qasource/qatarget setup — the
-- actual copy wired into Docker at container-init time is
-- services/mysql/init.sql (mounted read-only into
-- /docker-entrypoint-initdb.d/ by services/mysql/compose.yaml and run
-- against MYSQL_DATABASE=intdb). Keep the two in sync; this one exists under
-- sql/ so the harness can also (re-)apply it directly via a plain client
-- (e.g. mysql) against a reused/live MySQL that the framework didn't
-- provision — see services/mysql/service.yaml's live_override_env
-- (INT_MYSQL_HOST) — where there is no docker-entrypoint-initdb.d to mount
-- into.
--
-- Mirrors scripts/live/services/mysql/init-mysql.sql (the live-tier
-- container-init script) but simplified for integration use: no REPLICATION
-- grant, since these tests hit tables directly (DatabaseReader/Writer-style
-- access) rather than binlog-based CDC. If a future integration test
-- needs MySQLReader/CDC against this database, add REPLICATION to the
-- users below (see the live-tier file for the pattern).
--
-- qasource = source-side tables, qatarget = target-side tables; both can
-- login with password 'striim'. A source table T and a same-named target
-- table T can coexist as qasource.T / qatarget.T without colliding.
--
-- Run once per test database (idempotent-ish: re-running will error on
-- already-created users/schemas; DROP/recreate the database between full
-- resets instead of re-running this file against a live one).

-- Create users if they don't exist
CREATE USER IF NOT EXISTS 'qasource'@'%' IDENTIFIED BY 'striim';
CREATE USER IF NOT EXISTS 'qatarget'@'%' IDENTIFIED BY 'striim';

-- Create schemas (databases in MySQL terminology) if they don't exist
CREATE SCHEMA IF NOT EXISTS qasource;
CREATE SCHEMA IF NOT EXISTS qatarget;

-- Grant permissions to users for their respective schemas
GRANT ALL PRIVILEGES ON qasource.* TO 'qasource'@'%';
GRANT ALL PRIVILEGES ON qatarget.* TO 'qatarget'@'%';

-- Apply the grants
FLUSH PRIVILEGES;

-- mysql:8.0 runs with binary logging ON by default, and then a user without SUPER cannot
-- create a stored function (error 1419) -- which a case needs to fail a lookup on purpose
-- (lookup-retry-transient-recovery-mysql). Persisted so it survives a restart.
SET PERSIST log_bin_trust_function_creators = 1;
