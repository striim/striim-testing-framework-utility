# MySQL CDC Diff Test

Standard multi-database regression test: MySQL CDC via binlog (MySQLReader).

## Purpose

Verify that MySQLReader can capture binlog events and DatabaseWriter can write to target. Diff assertion confirms CDC replication.

## Test Flow

1. Create source and target tables (before app deployment)
2. Deploy Striim app with MySQLReader → DatabaseWriter
3. Seed source table post-start (inserts captured in binlog)
4. MySQLReader reads binlog events (CDC)
5. DatabaseWriter writes to target table
6. Diff assertion: verify target table data matches source table

## Key Difference from Non-CDC

- Seed runs **post_start** so inserts are captured in binlog
- MySQLReader starts from current binlog position after app deployment
- Only changes after app start are replicated (CDC semantics)

## Configuration

- Source: `${NS}.${TID}src` (seeded post-start: 3 rows)
- Target: `${NS}.${TID}tgt` (empty, populated by CDC)
- Timeout: 180 seconds
- Assertion: diff (source → target row matching)

## Running

```bash
pytest -m "live and mysql" scripts/live/regression/services/mysql/mysql-cdc-diff/test.yaml
```

## Expected Result

- App deploys with MySQLReader + DatabaseWriter
- Seed inserts 3 rows (post-start)
- Binlog captures inserts as CDC events
- DatabaseWriter applies events to target
- Diff assertion passes: target matches source

## Multi-Database Pattern

This test follows the standard CDC diff pattern established for postgres, oracle, etc.:
- Same structure (DDL, seed post-start, app.tql)
- Same database-agnostic naming (${TID}src, ${TID}tgt)
- Same assertion (diff between source and target)
- Same timeout and tags

Tests can run in parallel with other DB CDC tests without conflicts.

## Comparison

| Feature | mysql-diff | mysql-cdc-diff |
|---------|-----------|-----------------|
| Reader | DatabaseReader | MySQLReader |
| Seed | Pre-deployment | Post-deployment |
| Events | All rows | Binlog changes only |
| Real-time | No | Yes |
| Use Case | Initial load | Continuous sync |
