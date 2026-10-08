-- Build-time Oracle setup for the live-tier image (run by images/oracle/Dockerfile
-- during `docker build`, NOT at runtime). Enabling ARCHIVELOG needs a MOUNT-mode
-- restart, which at runtime fights gvenzl's PID-1 entrypoint (it re-orchestrates
-- startup on shutdown -> crash). Here there is no entrypoint monitor and we own the
-- single foreground sqlplus, so the restart is safe and the result (archivelog-enabled
-- control file + the CDC/data users) is baked into the image layer.
WHENEVER SQLERROR EXIT SQL.SQLCODE

STARTUP;

-- ARCHIVELOG (LogMiner prerequisite), via a MOUNT restart. SAVE STATE so FREEPDB1
-- auto-opens on every future (runtime) startup.
SHUTDOWN IMMEDIATE;
STARTUP MOUNT;
ALTER DATABASE ARCHIVELOG;
ALTER DATABASE OPEN;
ALTER PLUGGABLE DATABASE ALL OPEN;
ALTER PLUGGABLE DATABASE ALL SAVE STATE;

-- CDB root: DB-level supplemental logging (covers every table) + the c##striim common
-- LogMiner user (mining a PDB needs a common user with CONTAINER_DATA spanning
-- CDB$ROOT + the PDB; a local PDB user hits MissingLogminerPrivileges).
ALTER DATABASE ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS;
CREATE USER c##striim IDENTIFIED BY striim CONTAINER=ALL DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, EXECUTE_CATALOG_ROLE, SELECT_CATALOG_ROLE TO c##striim CONTAINER=ALL;
GRANT SELECT ANY TABLE, SELECT ANY TRANSACTION, SELECT ANY DICTIONARY TO c##striim CONTAINER=ALL;
GRANT LOGMINING, LOCK ANY TABLE, FLASHBACK ANY TABLE TO c##striim CONTAINER=ALL;
ALTER USER c##striim SET CONTAINER_DATA = (CDB$ROOT, FREEPDB1) CONTAINER=CURRENT;

-- This stored procedure doesn't seem to be necessary for the faststart images
-- BEGIN
--   DBMS_LOGMNR_D.BUILD(
--     OPTIONS => DBMS_LOGMNR_D.STORE_IN_REDO_LOGS
--   );
-- END;
-- /

-- These synonyms are required by Striim <5.4.0.2
CREATE SYNONYM SYSTEM.LOGMNR_COL$  FOR SYS.LOGMNR_COL$;
CREATE SYNONYM SYSTEM.LOGMNR_OBJ$  FOR SYS.LOGMNR_OBJ$;
CREATE SYNONYM SYSTEM.LOGMNR_USER$ FOR SYS.LOGMNR_USER$;
CREATE SYNONYM SYSTEM.LOGMNR_UID$  FOR SYS.LOGMNR_UID$;
GRANT SELECT ON SYSTEM.LOGMNR_COL$  TO c##striim;
GRANT SELECT ON SYSTEM.LOGMNR_OBJ$  TO c##striim;
GRANT SELECT ON SYSTEM.LOGMNR_USER$ TO c##striim;
GRANT SELECT ON SYSTEM.LOGMNR_UID$  TO c##striim;

-- QUIESCE's marker table, CDB$ROOT copy. THE CONTAINER IS THE WHOLE POINT.
--
-- The reader connects to the CDB ROOT -- service.yaml's ORACLE_CDC_URL uses cdb_service
-- FREE -- and c##striim is a COMMON user, so its schema exists separately in every
-- container. A copy in FREEPDB1 alone is invisible to the reader. This is not a guess: the
-- first attempt at this fix created the table in the PDB only, both gated-quiesce arms
-- failed again with the identical "Table {C##STRIIM.QUIESCEMARKER} not found", and they
-- passed only once a root copy existed. The fixtures say so too -- see the "must exist in
-- both CDB$ROOT and FREEPDB1" note in fixtures/gated-children/app_declared.tql.
--
-- Placement therefore matters more than the DDL: this block must stay ABOVE the
-- ALTER SESSION SET CONTAINER=FREEPDB1 below, because this file is ONE sqlplus session and
-- the container setting persists. Moved below it, this whole fix silently becomes a no-op.
--
-- Without the table Oracle does not FAIL a quiesce, it DISAPPROVES it and disables the
-- feature, so the app keeps running and the arm times out. Nothing else in this tier touches
-- the table, so its absence is invisible until a quiesce arm runs and then every Oracle
-- quiesce arm fails together. On the 2026-09-26 sweep that was 2 failures out of 102 arms.
--
-- The DDL is Oracle's own, verbatim from the message its reader prints when the table is
-- missing, so it stays whatever Striim expects rather than whatever seemed reasonable here.
CREATE TABLE c##striim.QUIESCEMARKER (
  source       VARCHAR2(100),
  status       VARCHAR2(100),
  sequence     NUMBER(10),
  inittime     TIMESTAMP,
  updatetime   TIMESTAMP DEFAULT SYSDATE,
  approvedtime TIMESTAMP,
  reason       VARCHAR2(100),
  CONSTRAINT quiesce_marker_pk PRIMARY KEY (source, sequence)
);

-- FREEPDB1: local data schemas. Readiness is gated by the container healthcheck
-- (gvenzl's /opt/oracle/healthcheck.sh + a qasource connect probe — see compose.yaml),
-- so no setup-complete sentinel table is needed.
ALTER SESSION SET CONTAINER=FREEPDB1;
CREATE USER qasource IDENTIFIED BY striim DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
CREATE USER qatarget IDENTIFIED BY striim DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
GRANT CONNECT, RESOURCE, CREATE TABLE, CREATE SESSION TO qasource;
GRANT CONNECT, RESOURCE, CREATE TABLE, CREATE SESSION TO qatarget;
GRANT SELECT ANY TABLE TO qasource;

-- QUIESCE's marker table, FREEPDB1 copy. Both containers are required (the fixtures say so),
-- and this is the container where qasource exists, so the grant belongs here and only here --
-- in CDB$ROOT it would fail, and WHENEVER SQLERROR EXIT above would fail the image build.
-- This pair reproduces exactly the state the two quiesce arms were observed to pass against.
CREATE TABLE c##striim.QUIESCEMARKER (
  source       VARCHAR2(100),
  status       VARCHAR2(100),
  sequence     NUMBER(10),
  inittime     TIMESTAMP,
  updatetime   TIMESTAMP DEFAULT SYSDATE,
  approvedtime TIMESTAMP,
  reason       VARCHAR2(100),
  CONSTRAINT quiesce_marker_pk PRIMARY KEY (source, sequence)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON c##striim.QUIESCEMARKER TO qasource;

SHUTDOWN IMMEDIATE;
EXIT
