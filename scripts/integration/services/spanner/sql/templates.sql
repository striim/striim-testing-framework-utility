-- HISTORICAL PLACEHOLDER -- NOT EXECUTED, DO NOT COPY VERBATIM.
-- The tier's actual convention: `${POSTGRES_SOURCE_SCHEMA}` (not the
-- `${PG_SOURCE_SCHEMA}` used below) is the real Postgres token, and Spanner tests
-- can't share it anyway -- Spanner has no schema concept, and `dbroutes`'s real
-- `spanner-google`/`spanner-postgres` routes carry no such token. Genuine
-- Spanner DDL fixtures should follow the bootstrap fixtures' pattern: a literal,
-- unprefixed-or-`${TID}`-prefixed table name per test, not this file's
-- schema-qualified naming. Left in place only as a reminder of the two-dialect
-- (GoogleSQL/PostgreSQL) DDL shape difference; see init.sql for what's real.
--
-- Reusable Spanner (GoogleSQL dialect) DDL templates for integration tests.
--
-- Token substitution (performed by the Python test harness before issuing
-- these as `database.update_ddl(...)` statements — Spanner has no SQL-side
-- substitution, and no psql/sqlplus-equivalent client to run this file
-- through directly; see init.sql):
--   ${PG_SOURCE_SCHEMA} -> reused from the Postgres tokens (NOT a
--                          Spanner-specific token) so the same qasource/
--                          qatarget naming convention lines up across
--                          Postgres and Spanner, per the Phase-1 token list.
--                          Normally "qasource".
--   ${TID}               -> same free-form, unique-per-test id prefix used
--                          for Postgres (GoogleSQL identifiers don't have
--                          Oracle's uppercase/30-byte constraints), e.g.
--                          qasource.test_lookup_by_keycustomers.
--
-- Target-side objects use the literal "qatarget" prefix/schema rather than
-- a token — every test's target lands under qatarget, only the
-- ${TID}-prefixed table name varies.
--
-- NOTE: GoogleSQL has no CREATE-schema-scoped namespacing the way Postgres
-- does; "qasource"/"qatarget" below are table-name prefixes (dotted just
-- like a schema-qualified name for readability/consistency with the
-- Postgres templates), not actual Spanner schemas. Phase 2 should firm up
-- whether this needs to become a real naming scheme or two databases (see
-- init.sql TODO).
--
-- These are intentionally minimal examples covering common column types.
-- Phase 2 operator/test suites should add their own template files (or
-- sections) alongside this one rather than growing this file unbounded.

-- Source-side table: common scalar types (INT64, STRING, TIMESTAMP,
-- NUMERIC, BOOL).
CREATE TABLE ${PG_SOURCE_SCHEMA}.${TID}customers (
  id         INT64 NOT NULL,
  name       STRING(100),
  email      STRING(100),
  balance    NUMERIC,
  is_active  BOOL,
  created_at TIMESTAMP,
) PRIMARY KEY (id);

-- Target-side table: same shape, under the (literal) qatarget prefix, for
-- writer-style tests that compare source vs. target rows after
-- replication.
CREATE TABLE qatarget.${TID}customers (
  id         INT64 NOT NULL,
  name       STRING(100),
  email      STRING(100),
  balance    NUMERIC,
  is_active  BOOL,
  created_at TIMESTAMP,
) PRIMARY KEY (id);

-- Cleanup companions (run at test teardown, before re-creating on retry).
-- DROP TABLE ${PG_SOURCE_SCHEMA}.${TID}customers;
-- DROP TABLE qatarget.${TID}customers;
