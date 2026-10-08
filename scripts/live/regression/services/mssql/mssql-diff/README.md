# mssql-diff

The SQL Server analogue of `postgres-diff`/`oracle-diff`: **DatabaseReader → DatabaseWriter**
within the shared `qauser` database, a non-CDC initial load, source and target tables in
their own `qasource`/`qatarget` schemas (`qasource.SRC` → `qatarget.TGT`). The reader
connects as `${MSSQL_SOURCE_USER}` (qasource), the writer as `${MSSQL_TARGET_USER}`
(qatarget) — both `db_owner` members, so no `sa` credentials are needed in the app. The
source is seeded **before** deploy; the reader queries it, the writer propagates, and the
diff tier (source routed via `source_db: mssql-source`, target via `target_db: mssql-target`)
asserts `qatarget.TGT` catches up to `qasource.SRC`.

Requires the MSSQL service (`requires: [mssql]`); no CDC / SQL Server Agent involved — this
validates the plain SQL Server reader/writer path and the `MssqlAdmin` DDL/seed/read wiring.
