# mysql-parallel-cdc

Verifies that MySQL tests isolate from one another under concurrent workers.

## What it tests

- Multiple tests run concurrently without table conflicts
- Table-name isolation via the `${TID}` prefix
- No cross-test data contamination
- CDC replication still works under parallel execution

## Isolation model

Schemas are **fixed** — `qasource` and `qatarget` — in every mode, shared by all
workers. There is no per-test schema. Isolation is by table name.

`${TID}` is a per-test **prefix** carrying its own trailing underscore:

| Mode | `${TID}` | `${TID}parallel_source` |
|------|----------|-------------------------|
| Serial | empty | `parallel_source` |
| Parallel | `t<9 hex>_` | `t0a1b2c3d4_parallel_source` |

It is hashed from the test name (`plugin.py`, `_tid_oracle`), not taken from the
xdist worker id, so it is stable across reruns of the same test. There is no
`gw0`/`gw1` form.

Both tables carry it:

- Source: `qasource.${TID}parallel_source`
- Target: `qatarget.${TID}parallel_test_table`

The target must be prefixed too. While it was a fixed name, every worker wrote to
one shared table and the test did not actually demonstrate isolation.

## Tokens

- `${MYSQL_SOURCE_SCHEMA}` / `${MYSQL_TARGET_SCHEMA}` — `qasource` / `qatarget`
- `${MYSQL_SOURCE_USER}` / `${MYSQL_TARGET_USER}` — `qasource` / `qatarget`
- `${TID}` — per-test table prefix, as above
- `${NS}` — Striim namespace for the app, not a database schema

## Seed data

Three rows tagged `worker_${TID}`, so contamination between tests would be visible
in the data as well as in the row count.

## Running

```bash
cd scripts/live
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1

# serial
pytest -m "live and mysql" regression/services/mysql/mysql-parallel-cdc -v

# four workers, whole MySQL suite
SLT_PARALLEL=1 pytest -m "live and mysql" regression/services/mysql -n 4 -v
```

## Inspecting

```bash
docker exec slt-mysql mysql -u root -pstriim -e "SHOW TABLES IN qasource;"
docker exec slt-mysql mysql -u root -pstriim -e "SHOW TABLES IN qatarget;"
```

Under `-n 4` you will see four differently-prefixed table sets in the same two
schemas — that is the isolation working.

## Cleanup

- Setup, parallel: `reset_test_objects(tid)` drops only this test's prefix
- Setup, serial: `reset_schemas()` drops and recreates both schemas
- Teardown: `drop_test_tables(prefix)` — the prefix in parallel runs, `""` (whole
  schema) in serial

All three keep the tables whose name starts with the tid, which is why the prefix
form is required.
