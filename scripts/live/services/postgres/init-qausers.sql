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
