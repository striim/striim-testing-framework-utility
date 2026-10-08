# MySQL Diff Test

Standard multi-database regression test: MySQL initial load via DatabaseReader/Writer.

## Purpose

Verify that DatabaseReader can read MySQL source table and DatabaseWriter can write to target table. Diff assertion confirms data replication.

## Test Flow

1. Create source and target tables
2. Seed source table (before app deployment)
3. Deploy Striim app with DatabaseReader → DatabaseWriter
4. Diff assertion: verify target table data matches source table

## Configuration

- Source: `${NS}.${TID}src` (3 rows: alpha, bravo, charlie)
- Target: `${NS}.${TID}tgt` (empty, populated by replication)
- Timeout: 180 seconds
- Assertion: diff (source → target row matching)

## Running

```bash
pytest -m "live and mysql" scripts/live/regression/services/mysql/mysql-diff/test.yaml
```

## Expected Result

- App deploys successfully
- DatabaseReader reads 3 rows from source
- DatabaseWriter writes 3 rows to target
- Diff assertion passes: target matches source

## Multi-Database Pattern

This test follows the standard diff pattern established for postgres, oracle, mssql, and spanner:
- Same structure (DDL, seed, app.tql)
- Same database-agnostic naming (${TID}src, ${TID}tgt)
- Same assertion (diff between source and target)
- Same timeout and tags

Tests can run in parallel with other DB variants without conflicts.
