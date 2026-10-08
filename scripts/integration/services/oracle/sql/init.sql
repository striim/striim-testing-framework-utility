-- Oracle user setup for the integration test tier.
--
-- Base-image setup only: creates the QASOURCE/QATARGET data users with just
-- enough privilege to create and own tables. Deliberately does NOT enable
-- ARCHIVELOG or configure LogMiner/supplemental logging (compare
-- scripts/live/services/oracle/images/oracle/bake.sql, which does that for
-- CDC/PostgreSQLReader-equivalent live testing) — integration tests in this
-- tier exercise plain DML/DDL against the database, not change-data-capture.
-- If a Phase 2 suite needs Oracle CDC, add the ARCHIVELOG + c##striim
-- LogMiner setup from bake.sql to a separate init step rather than here.
--
-- Run against the pluggable database (e.g. FREEPDB1) that the integration
-- harness targets. QASOURCE = source-side tables, QATARGET = target-side
-- tables; both IDENTIFIED BY 'striim'.
WHENEVER SQLERROR EXIT SQL.SQLCODE

-- gvenzl's /container-entrypoint-initdb.d hook runs this as `sqlplus -s / as
-- sysdba` against CDB$ROOT. Without this ALTER SESSION the users below are
-- created as COMMON users in the root container, and every GRANT lands in
-- CON_ID 1 (root) only -- so QASOURCE/QATARGET exist in FREEPDB1 with NO
-- privileges at all (every connection attempt fails ORA-01045: does not have
-- CREATE SESSION privilege). Mirrors scripts/live/services/oracle/images/
-- oracle/bake.sql. A healthcheck must match a complete result line: a bare
-- `grep -q 1` also matches the "1" inside "ORA-01045" and reports a broken
-- service as healthy.
ALTER SESSION SET CONTAINER=FREEPDB1;

CREATE USER QASOURCE IDENTIFIED BY striim DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
CREATE USER QATARGET IDENTIFIED BY striim DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;

GRANT CONNECT, RESOURCE TO QASOURCE;
GRANT CONNECT, RESOURCE TO QATARGET;

-- RESOURCE already implies CREATE TABLE/SEQUENCE/PROCEDURE/TRIGGER etc, but
-- these are spelled out explicitly since RESOURCE's implied grants vary by
-- Oracle version and this is what templates.sql actually needs.
GRANT CREATE TABLE, CREATE SEQUENCE, CREATE SESSION TO QASOURCE;
GRANT CREATE TABLE, CREATE SEQUENCE, CREATE SESSION TO QATARGET;

-- Lets DatabaseReader-style source tests (and any cross-schema template
-- verification) read QASOURCE's tables without per-table grants.
GRANT SELECT ANY TABLE TO QASOURCE;
