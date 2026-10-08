-- Reusable Oracle DDL templates for integration tests.
--
-- Token substitution (performed by the Python test harness before executing
-- these statements — Oracle itself does no substitution):
--   ${ORACLE_SOURCE_SCHEMA} -> the source-side user/schema, normally
--                              "QASOURCE" (see init.sql).
--   ${TID_ORACLE}           -> a short, unique-per-test id prefix, uppercase
--                              and hash-shortened (unlike the free-form
--                              ${TID} used for Postgres/Spanner) to respect
--                              Oracle's unquoted-identifier rules (uppercase,
--                              alphanumeric/underscore) and the 30-byte
--                              identifier limit on older-compatibility
--                              databases, e.g. T0A1B2C3 rather than
--                              TEST_LOOKUP_BY_KEY. Yields e.g.
--                              QASOURCE.T0A1B2C3CUSTOMERS.
--
-- Target-side objects use the literal QATARGET schema (created by init.sql)
-- rather than a token — every test's target lands in the same QATARGET
-- schema, only the ${TID_ORACLE}-prefixed table name varies.
--
-- These are intentionally minimal examples covering common column types.
-- Phase 2 operator/test suites should add their own template files (or
-- sections) alongside this one rather than growing this file unbounded.

-- Source-side table: common scalar types (NUMBER, VARCHAR2, DATE, TIMESTAMP).
CREATE TABLE ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}CUSTOMERS (
  ID         NUMBER PRIMARY KEY,
  NAME       VARCHAR2(100),
  EMAIL      VARCHAR2(100),
  BALANCE    NUMBER(12, 2),
  IS_ACTIVE  NUMBER(1),
  CREATED_AT TIMESTAMP,
  SIGNED_UP  DATE
);

-- Target-side table: same shape, in the (literal) QATARGET schema, for
-- DatabaseWriter/Mapping-style tests that compare source vs. target
-- rows after replication.
CREATE TABLE QATARGET.${TID_ORACLE}CUSTOMERS (
  ID         NUMBER PRIMARY KEY,
  NAME       VARCHAR2(100),
  EMAIL      VARCHAR2(100),
  BALANCE    NUMBER(12, 2),
  IS_ACTIVE  NUMBER(1),
  CREATED_AT TIMESTAMP,
  SIGNED_UP  DATE
);

-- Cleanup companions (run at test teardown, before re-creating on retry).
-- DROP TABLE ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}CUSTOMERS;
-- DROP TABLE QATARGET.${TID_ORACLE}CUSTOMERS;
