# Writing tests

This walks through one test from an empty folder to a run. The example app is a change-data-capture
(CDC) pipeline: `PostgreSQLReader` reads an `orders` table and `DatabaseWriter` applies every insert,
update and delete to a copy. The test inserts three orders, updates one, deletes one, and checks that
the copy ends up with exactly the two orders that are left.

Each step is short. The reference for every key is [TEST-YAML.md](TEST-YAML.md).

Before you start, finish [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md), so that `striim-test doctor`
passes and `samples/live/01-plain-replication` runs. The commands below run from the clone root, with the
virtual environment active.

## 1. Copy the closest sample

A test is a folder with a `test.yaml` and the files it names. Start from the sample that looks most like
your app:

| Your app | Copy |
|---|---|
| an initial load, table to table | `samples/live/01-plain-replication` |
| an initial load with a CQ that changes the rows | `samples/live/02-transform` |
| writes a file | `samples/live/03-file-output` |
| CDC: reads changes while it runs | `samples/live/04-lifecycle-check` |
| a MySQL source and target | `samples/live/05-mysql-initial-load` or `06-mysql-cdc` |
| loads your own Java (Open Processor or UDF) | `samples/code/op` or `samples/code/udf` (step 8) |

The orders app is CDC, so copy `04-lifecycle-check`. Put it under `samples/`, because `striim-test run`
only runs cases under `SLT_LIVE_CASES` (`samples` in `.env.example`):

```
mkdir -p samples/mine
cp -r samples/live/04-lifecycle-check samples/mine/orders-cdc
rm samples/mine/orders-cdc/README.md
```

Once your tests live in a repo of your own (step 9), they go there instead.
To keep your tests in a folder of their own instead, list both roots:
`SLT_LIVE_CASES=samples:/path/to/my-tests` (separated by `:`; case names must be unique across them).

## 2. Put your TQL in

Writing or changing the app itself is covered in [tql/README.md](tql/README.md).

Replace `app.tql` with your app. Keep your sources, CQs and targets. Change only the names and the
connection details, to tokens the framework fills in for each run:

<!-- snippet: file orders-cdc/app.tql -->
```sql
CREATE NAMESPACE ${NS};
USE ${NS};

CREATE OR REPLACE APPLICATION ${APP};

CREATE OR REPLACE SOURCE OrdersCdc USING Global.PostgreSQLReader (
  ConnectionURL: '${PG_URL}',
  Username: '${PG_SOURCE_USER}',
  Password: '${PG_SOURCE_PASSWORD}',
  Tables: '${PG_SOURCE_SCHEMA}.${TID}orders',
  ReplicationSlotName: '${PG_SLOT}'
) OUTPUT TO OrderChanges;

CREATE OR REPLACE TARGET OrdersCopy USING Global.DatabaseWriter (
  ConnectionURL: '${PG_URL}',
  Username: '${PG_TARGET_USER}',
  Password: '${PG_TARGET_PASSWORD}',
  Tables: '${PG_SOURCE_SCHEMA}.${TID}orders,${PG_TARGET_SCHEMA}.${TID}orders_copy',
  BatchPolicy: 'EventCount:100,Interval:1',
  CommitPolicy: 'EventCount:100,Interval:1'
) INPUT FROM OrderChanges;

END APPLICATION ${APP};

DEPLOY APPLICATION ${APP};
START APPLICATION ${APP};
```

What the tokens do, and the rules an app follows ("Rules every test follows", below, has the
rest):

- **`${NS}` and `${APP}`.** Each run gets its own namespace and app name. Keep the
  `CREATE NAMESPACE`, `USE`, `DEPLOY` and `START` lines: the framework imports the whole file.
- **`${TID}` before every table name.** It keeps two runs, or two tests running at once, apart. Use
  it in the TQL, the SQL files and `test.yaml` alike. For Oracle use `${TID_ORACLE}`.
- **Connection tokens instead of hosts and passwords.** `${PG_URL}`, `${PG_SOURCE_USER}` and the
  rest come from the service the test requires (step 3). [SERVICES.md](SERVICES.md) lists
  every service's tokens.
- **`${PG_SLOT}` for the replication slot**, so each run has its own.

What an app must not do:

- hard-code a host, port, user, password, table, topic, bucket or file name;
- create a schema or a publication in its `ddl:` files: the framework refuses both before running
  the file, because it creates and removes the schemas a test uses;
- put its components in any namespace but `${NS}`, which must not exist before the run;
- use a component name (source, CQ, target) longer than 21 characters. A component's full name is
  `<namespace>.<name>`, and some adapters fail at `START` when it reaches 64 characters
  (SpannerWriter: "Insufficient Privilege to get Dialect");
- write files anywhere but under `${OWNED_DIR}/` when the test has a `lifecycle:` block (step 5).

## 3. Declare the services, or use your own database

`requires:` in `test.yaml` names the services the test needs. The framework starts each one in
Docker, with test-only accounts, or reuses it if it is already running:

<!-- snippet: fragment -->
```yaml
requires: [postgres]
```

The shipped services are `postgres`, `oracle`, `mssql`, `mysql`, `vertica`, `kafka`, `spanner` and `gcs`.
`teradata` is there too, as a connection to your own instance only (no container ships);
[WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md) says what each can do.

To run against your own Postgres instead, set its host and accounts in `.env`. The test does not
change:

```
SLT_PG_HOST=db.example.com
SLT_PG_ADMIN_USER=postgres
SLT_PG_ADMIN_PASSWORD=...
SLT_PG_SOURCE_USER=qasource
SLT_PG_SOURCE_PASSWORD=...
SLT_PG_TARGET_USER=qatarget
SLT_PG_TARGET_PASSWORD=...
```

Use a disposable database: the framework creates the source and target roles and schemas if they
are missing, and drops the objects each test creates. For CDC it also needs `wal_level=logical`, the `wal2json` plugin, and the `REPLICATION`
attribute on the source user. [SERVICES.md](SERVICES.md) has every service's settings. For a
database or system the framework does not ship, see [YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md).

## 4. Add the tables, the data and the expected result

**Tables.** One DDL file per side. `ddl:` runs each file on its route: `postgres-source` is the
`qasource` schema and `postgres-target` the `qatarget` schema.

<!-- snippet: file orders-cdc/ddl_source.sql -->
```sql
CREATE TABLE ${TID}orders (
  id integer PRIMARY KEY,
  customer varchar(64) NOT NULL,
  amount numeric(12, 2) NOT NULL,
  status varchar(16) NOT NULL
);
```

<!-- snippet: file orders-cdc/ddl_target.sql -->
```sql
CREATE TABLE ${TID}orders_copy (
  id integer PRIMARY KEY,
  customer varchar(64) NOT NULL,
  amount numeric(12, 2) NOT NULL,
  status varchar(16) NOT NULL
);
```

Keep the sample's `slot.sql` as it is. It creates the replication slot, in its own file because
Postgres cannot create a slot after a write in the same transaction.

**Data.** A CDC reader only sees changes made after it starts, so this data is a `seed:` file with
`when: post_start`. An initial-load app reads what is already there, so its seed runs `pre_deploy`
(the default). Replace the sample's `changes.sql`:

<!-- snippet: file orders-cdc/changes.sql -->
```sql
BEGIN;
INSERT INTO ${TID}orders (id, customer, amount, status) VALUES
  (1001, 'Record01', 25.00, 'new'),
  (1002, 'Record02', 7.50, 'new'),
  (1003, 'Record03', 120.25, 'new');
COMMIT;

BEGIN;
UPDATE ${TID}orders SET status = 'shipped', amount = 118.00 WHERE id = 1003;
COMMIT;

BEGIN;
DELETE FROM ${TID}orders WHERE id = 1002;
COMMIT;
```

**The expected result.** Write it by hand from the data, never by copying a run's output. It is
what the target must hold at the end: order 1002 is gone and 1003 is shipped.

<!-- snippet: file orders-cdc/expected/rows.csv -->
```csv
id,customer,amount,status
1001,Record01,25.00,new
1003,Record03,118.00,shipped
```

Delete the sample's old golden if its name differs from yours. Here it is `expected/rows.csv` in
both, so the file above replaces it.

## 5. Say how to compare, and how to know the app has finished

The whole `test.yaml`:

<!-- snippet: manifest orders-cdc/test.yaml -->
```yaml
name: orders-cdc
purpose: Postgres CDC applies inserts, an update and a delete to the orders copy
tql: app.tql
topology: single
requires: [postgres]
timeout: 180
tags: [cdc, postgres, orders]
ddl:
  - file: ddl_source.sql
    db: postgres-source
  - file: ddl_target.sql
    db: postgres-target
  - file: slot.sql
    db: postgres-source
seed:
  - file: changes.sql
    db: postgres-source
    when: post_start
exact: {version: 1}
assert:
  smoke: true
  data:
    - target: "${PG_TARGET_SCHEMA}.${TID}orders_copy"
      target_db: postgres-target
      match: expected/rows.csv
      exact:
        columns: {id: integer, amount: "decimal:2"}
lifecycle:
  version: 1
  mode: cdc
  sink: db
  readiness: {kind: sentinel}
  completion: {kind: sentinel}
  stability: 5s
  deadlines: {readiness: 90s, completion: 120s}
  reset: owned
  sentinel:
    db: postgres-source
    insert: sentinel_insert.sql
    delete: sentinel_delete.sql
    observe:
      db: postgres-target
      table: "${PG_TARGET_SCHEMA}.${TID}orders_copy"
      key: id
```

`name:` must be unique across your tests. `purpose:` is one line saying what the test proves.

**`exact:`: compare every row, by value.** Without it, `match:` compares the distinct set of rows
as text: a duplicated row passes, and `118.0` is not `118.00`. With the top-level `exact:` block
and `exact:` on the spec, the comparison counts every row, so a missing, extra, duplicated or changed
row fails. `columns:` gives a column's type, so values compare by meaning rather than by how they
are printed: `amount` as a decimal with two places. A column the database returns as a number,
a date or JSON must be typed, or the run stops with `undeclared-conversion:<column>` naming it.
Text columns need nothing. [TEST-YAML.md](TEST-YAML.md#exact--compare-rows-exactly) lists the types.

**`lifecycle:`: know when the app has finished.** Without it, the framework polls the assertion
until it passes or `timeout` runs out. That cannot tell "done" from "not started": an empty or
half-copied target can match too early. The `lifecycle:` block makes the framework wait for proof:

- `readiness` and `completion` are `sentinel`: the framework inserts a row with an id unique to
  this run, waits until it appears on the target, deletes it and waits until it is gone. At
  readiness this proves the reader is capturing. At completion, after `changes.sql`, it proves
  every change before it has arrived, because CDC keeps commit order.
- `stability: 5s`: the target must then stay unchanged for five seconds.
- `reset: owned`: the framework removes only what this run created.

The sentinel needs two small SQL files. Your table has `NOT NULL` columns, so fill them:

<!-- snippet: file orders-cdc/sentinel_insert.sql -->
```sql
INSERT INTO ${TID}orders (id, customer, amount, status) VALUES (${SENTINEL_ID}, 'sentinel', 0, 'sentinel');
```

<!-- snippet: file orders-cdc/sentinel_delete.sql -->
```sql
DELETE FROM ${TID}orders WHERE id = ${SENTINEL_ID};
```

For an initial load, use `mode: initial-load` with `baseline-landed` and `source-count`, as in
`01-plain-replication`. For an app that writes a file, see `03-file-output`.
[TEST-YAML.md](TEST-YAML.md#lifecycle--prove-the-app-finished) has every kind.

## 6. Check it, list it, run it

**Check the test without running it.** `doctor` loads each case you name the way a run does,
and prints one `case` line for it. A mistake shows there with the key at fault:

```
striim-test doctor --case samples/mine/orders-cdc
```

```
[ ok ] case orders-cdc: test.yaml loads (orders-cdc)
```

A case that is not under `SLT_LIVE_CASES` shows `not checked: outside every case root` instead:
move it, or set `SLT_LIVE_CASES`. Doctor also checks your `.env`, Striim and the services the case requires. Fix what those lines
name before you run.

**See what a run would select.** `list` prints every case under `SLT_LIVE_CASES`. `--dry-run`
prints the selection for one path without running it:

```
striim-test list
striim-test run samples/mine/orders-cdc --dry-run
```

Both print the case as `live:samples/mine/orders-cdc::orders-cdc`.

**Run it.** This needs Striim and Docker:

```
striim-test run samples/mine/orders-cdc
```

Exit code 0 means it passed. The others: 1 a test failed, 2 a mistake in the settings or in a
`test.yaml`, 3 something it needs was not available (the test was skipped, which is not a pass),
4 cancelled or timed out, 5 nothing was selected.

## 7. Read a failure

The first line of the output names the run directory:

```
striim-test: run-dir: .../scripts/live/runs/20260925T143221Z-d486c80b
```

In it, under `live/`:

| File | What is in it |
|---|---|
| `stdout.log` | pytest's output: each phase as it ran, and the failure message |
| `junit.xml` | the result per case, for CI |
| `junit.slt.json` | each assertion's result and detail, or the skip reason |
| `evidence/<case>/<run>/evidence.json` | the full record of the case, below |
| `cluster-logs/` | the Striim nodes' logs, when the cluster never answered (Docker mode) |

`evidence.json` is the place to look first. The parts you will read:

- `run.status`, `run.failure` and `run.skipReason`: what happened, in one line.
- `lifecycle.ready` and `lifecycle.completion`: which witness was satisfied, when, and the last
  observations it made. `reason` is `deadline` when it timed out, `terminal-status:HALT` when the
  app halted, `zero-count` when there was nothing to count.
- `data.comparisons[]`: one entry per exact comparison. `equal` says whether it matched, and
  `samples.missing` and `samples.extra` list the rows that differ, with their counts.
- `resources`: what the run created, reused and cleaned up.

[TROUBLESHOOTING.md](TROUBLESHOOTING.md) lists the common failures, what causes each, and the fix.

To look at the database after a failure, run with `--keep-resources`: the tables, slot and app stay
in place. `striim-test doctor` then names the command that removes them
(`python -m livetest.ownership replay <ledger>`).

## 8. A test that loads your own Java

An app that uses your Open Processor or UDF names the Maven module, and the framework builds the jar
and loads it into Striim before the app deploys. Copy `samples/code/op` or `samples/code/udf`;
[TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md) covers it in full. The keys that differ from an app-only
test:

<!-- snippet: fragment -->
```yaml
example: samples/code/op/java/OpenProcessors/ReferenceOp/examples/copy-adds-userdata
tql: app.tql
op:
  jar: samples/code/op/java/OpenProcessors/ReferenceOp
```

- `op:` loads the jar with `LOAD OPEN PROCESSOR`; `udf:` loads a UDF library. `jar:` is the module
  directory, which must sit under a `java/OpenProcessors/` directory for `op:` or
  `java/UserDefinedFunctions/` for `udf:`. The OP sample's TQL names the processor
  `Global.${OP_NAME}`, the loaded jar's name. The UDF sample calls its functions by name.
- `example:` is the folder that holds the app's TQL and SQL files. Only `expected/` stays next to
  `test.yaml`.
- Both paths are relative to the project root: `SLT_PROJECT_ROOT`, or this clone.
- You need Maven, a JDK 17 and `STRIIM_HOME`. Against your own Striim server, `STRIIM_HOME` must be
  that server's own install, on this machine.

## 9. Move your tests into your own repo

When the test works, keep it with your app rather than in this clone.
[SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md) sets up a repo of your own, with the framework
pinned to one version, from a starter template. A repo's `gold-targets.yaml` can also list
`servicesRoots:`, for services your tests need that the framework does not ship:
[YOUR-OWN-SERVICES.md](YOUR-OWN-SERVICES.md).

## Rules every test follows

- **One behaviour.** A test proves one observable behaviour, and its `purpose:` says which, in one
  line, present tense. One behaviour can still load several jars, if the scenario chains them.
- **One app.** `tql:` names exactly one file, inside the test (or its `example:` folder).
- **Nothing hard-coded.** Hosts, ports, users, passwords, tables, topics, buckets and file names
  come from tokens, and every object name starts with `${TID}` (`${TID_ORACLE}` on Oracle).
- **The expected result is written by hand** from the input and what the app should do. Never
  copy it from a run's output: a golden learned from the app proves only that the app did the
  same thing twice.

The framework does not check the first rule; review your tests against it.

## Patterns

**A CDC app.** The data the app should capture goes in a `seed:` entry with `when: post_start`, so
it is written after the reader has started. `after:` delays it further. Prove the app caught up with
the sentinel `lifecycle:` (step 5).

<!-- snippet: fragment -->
```yaml
seed:
  - file: baseline.sql
    db: postgres-source
    when: pre_deploy
  - file: changes.sql
    db: postgres-source
    when: post_start
    after: 10s
```

**An app that should halt.** `expect_halt: true` passes when the app reaches a terminal status;
`expect_halt_contains` also checks why. Pick a substring unique to your test (a table name), not a
generic phrase.

<!-- snippet: fragment -->
```yaml
expect_halt: true
expect_halt_contains: "orders_copy"
```

**Surviving a restart.** `recover:` stops (or quiesces, or in Docker mode kills) the app after the
`post_start` data, brings it back, and then asserts, so the assertions describe what survived.
Interrupt more than once: the first stop often looks clean.

<!-- snippet: fragment -->
```yaml
recover:
  mode: stop
  after: 3
  times: 2
  every: 12
assert:
  diff:
    - source: ${PG_SOURCE_SCHEMA}.${TID}src
      source_db: postgres-source
      target: ${PG_TARGET_SCHEMA}.${TID}tgt
      target_db: postgres-target
```

**A known defect.** `xfail:` keeps a test that reproduces a bug, asserting the correct behaviour,
and reports it as an expected failure on the tiers you name. Anything else that fails (a deploy
error, an app that never started) is still a failure.

<!-- snippet: fragment -->
```yaml
xfail:
  reason: "a stop mid-batch loses the rest of the batch"
  tiers: [diff]
```

**An initial load followed by CDC.** If an app combines `DatabaseReader` and `PostgreSQLReader`
writing the same target, keep the source quiescent until its initial load has landed. For a small
test fixture, list the source and target table files, a baseline insert file, and then `slot.sql`
under `ddl:`, in that order. These setup files run separately before deploy: committing the baseline
before creating the slot keeps CDC from replaying the baseline as duplicate inserts. Ordinary
change data still belongs in `seed:` with `when: post_start`.

Use `mode: initial-load` with readiness `baseline-landed` to hold the changes until the baseline
lands, then completion `row-count` with a positive final count different from the baseline, plus
stability and a typed exact golden. This tests a controlled startup, not a snapshot taken while
writes continue; it does not provide a seamless snapshot/CDC handoff. The
[Acme Retail example](../examples/acme-retail/README.md) shows the complete app and case.

**One app, several tests.** `example:` can point at an ordinary app directory as well as an OP/UDF
example. Its TQL, DDL and seed paths resolve there; the golden stays beside `test.yaml`.
Two tests can point at the same app folder with `example:` and differ
in their data or assertions; a value that differs goes in `tokens:` and the TQL uses `${NAME}`.

<!-- snippet: fragment -->
```yaml
tokens:
  REGION: emea
```

[TEST-YAML.md](TEST-YAML.md) has the rest: actions while the app runs (`action:`), files placed on
the Striim server (`server_files:`), and every assertion tier.

## Organising a suite

- **`tags:`** are free-form labels for your own reporting.
- **`depth:`** is one of `smoke`, `gate`, `regression` (the default), `fault`, `customer`,
  `measure`, `canary`. It becomes a pytest marker, `depth_<value>`. `striim-test run` selects by
  path or `--case`, not by depth, so keep a folder per group you want to run together, or run the
  engine with pytest directly ([internals/ENGINE.md](internals/ENGINE.md)).
- **`disabled: "<reason>"`** skips a test before anything is provisioned; `SLT_RUN_DISABLED=1` runs
  it anyway. `disabled_parallel:` skips only in a parallel run.

## Find an example of any key

The framework tests itself with one small live test per `test.yaml` feature, in
`scripts/live/regression/framework/`. Its [README](../scripts/live/regression/framework/README.md)
lists which folder shows which key.
