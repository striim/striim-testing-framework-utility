-- Reusable Postgres DDL templates for integration tests.
--
-- Token substitution (performed by the Python test harness before executing
-- these statements against the database — Postgres itself does no
-- substitution):
--   ${PG_SOURCE_SCHEMA}  -> the source-side schema, normally "qasource"
--                           (see init.sql). Templated (rather than hardcoded)
--                           so a test can point source objects at an
--                           alternate schema if it ever needs to.
--   ${TID}                -> a short, unique-per-test id prefix (e.g.
--                           "test_lookup_by_key") so tables from concurrent
--                           or repeated test runs never collide, e.g.
--                           qasource.test_lookup_by_keycustomers.
--
-- Target-side objects use the literal "qatarget" schema (created by
-- init.sql) rather than a token — every test's target lands in the same
-- qatarget schema, only the ${TID}-prefixed table name varies.
--
-- These are intentionally minimal examples covering common column types.
-- Phase 2 operator/test suites should add their own template files (or
-- sections) alongside this one rather than growing this file unbounded.

-- Source-side table: common scalar types (INT, VARCHAR, TIMESTAMP, DECIMAL,
-- BOOLEAN).
CREATE TABLE ${PG_SOURCE_SCHEMA}.${TID}customers (
  id         INT PRIMARY KEY,
  name       VARCHAR(100),
  email      VARCHAR(100),
  balance    DECIMAL(12, 2),
  is_active  BOOLEAN,
  created_at TIMESTAMP
);

-- Target-side table: same shape, in the (literal) qatarget schema, for
-- DatabaseWriter/Mapping-style tests that compare source vs. target
-- rows after replication.
CREATE TABLE qatarget.${TID}customers (
  id         INT PRIMARY KEY,
  name       VARCHAR(100),
  email      VARCHAR(100),
  balance    DECIMAL(12, 2),
  is_active  BOOLEAN,
  created_at TIMESTAMP
);

-- Cleanup companions (run at test teardown, before re-creating on retry).
-- DROP TABLE IF EXISTS ${PG_SOURCE_SCHEMA}.${TID}customers;
-- DROP TABLE IF EXISTS qatarget.${TID}customers;
