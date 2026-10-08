# Troubleshooting

Find what you see in the left column. Start with `striim-test doctor --case <your case>`: it checks
your settings, Striim, the services a case requires and the case itself, and names what to change.
After a run, the run directory's `live/evidence/<case>/<run>/evidence.json` says what happened in
more detail than the console ([WRITING-TESTS.md](WRITING-TESTS.md), "Read a failure").

## Where the logs are

`striim-test run` prints the run directory first (`run-dir: …`) and the `tail -f` command that
follows the run. At the end it prints how many tests passed and failed, each failed test with its
one-line reason, and the exit line: a run that passed ends with `live: ok (exit 0)`. Everything
else is in the run directory, `.state/runs/<run-dir>/` in your repository:

| File | What it holds | Read it when |
|---|---|---|
| `outcome.json` | the exit code and, per tier, the reason: `ok`, `tests-failed`, … | you want the verdict without the console |
| `live/results.json` | the tests that passed, failed or were skipped; each failure with its one-line message | the console has scrolled away |
| `live/stdout.log` | the whole output: each test's phases, then every failure in full at the end | a failure's one line is not enough |
| `live/junit.xml`, `live/junit.slt.json` | per test: status, duration, each assertion with what it expected and found | a CI system or a script reads the result |
| `live/evidence/<case>/<run>/evidence.json` | one test in detail: services, assertions, cleanup | an assertion failed and you need the rows |
| `cluster-logs/` | each Striim node's log | the Docker cluster never answered (exit 3) |

The services a case requires, and in Docker mode the Striim cluster, run as containers whose names
start with `slt-` (with `SLT_STACK_PREFIX` set, `<prefix>-slt-`):

```bash
docker ps -a --filter name=slt-     # every container, running or stopped, with its state
docker logs --tail 100 slt-kafka    # why a service stopped or would not start
docker logs --tail 100 slt-node     # the Striim server log of the node that runs the apps
```

`slt-striim` is the cluster's first node and `slt-agent` its Forwarding Agent; `docker logs` reads
either the same way.

## Before anything runs

| You see | Why | Fix |
|---|---|---|
| `pip` stops: `requires a different Python: 3.11.9 not in '>=3.12'` | the venv was made with a Python older than 3.12; on a Mac, `python3` is often 3.9 or 3.11 | `rm -rf .venv`, then make it with a newer one: `python3.12 -m venv .venv`. In your own repo, `python3 scripts/sync-framework.py` picks one for you ([SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md)) |
| `sync-framework: … has Python 3.11.9, and the framework at … needs Python 3.12 or later` (exit 2) | the same, caught before installing | `rm -rf .venv` and run `python3 scripts/sync-framework.py` again; with no Python 3.12 or later installed, install one or pass `--python PATH` |
| `cp: .env.example: No such file or directory`, or no `.env.example` in Finder | files that start with a dot are hidden; or you are not in the folder that has it | `ls -a` shows it. In your own repo, `python3 scripts/sync-framework.py` makes `.env` for you |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | something else on this machine uses the port a service publishes on | set another in `.env`, for Postgres `SLT_PG_HOST_PORT=55432`; every service's setting is in [SERVICES.md](SERVICES.md). `doctor` names it |
| `PermissionError: … /tmp/slt-locks/…`, or "the shared lock dir … belongs to …" | every user on the machine shares one lock directory, created writable only by its first user by an older version | that user runs `chmod 1777 /tmp/slt-locks`; or `export SLT_LOCK_DIR=$HOME/.slt-locks` (shell only) and do not run at the same time as them |
| a run refuses to start: `SLT_INFRA_OWNERSHIP` | the ownership setting is missing | keep `SLT_INFRA_OWNERSHIP=shared` and `SLT_KEEP_SERVICES=1` from `.env.example`. With pytest directly, export them: pytest does not read `.env` |
| a test fails before it starts: `docker compose up failed for kafka … container slt-kafka exited (1)`, and `docker logs slt-kafka` ends with `NodeExistsException` | a stopped Kafka stack was started again while its ZooKeeper still held the stopped broker's registration, which lasts 18 s. A framework checkout elsewhere on the machine shares the `slt-` stack, so its stopped containers count | run again: the framework starts a stopped Kafka stack from clean state. With a framework pinned before that, `docker rm -fv slt-kafka slt-zookeeper`, then run again |
| `striim: … Striim needs a license to boot` | Docker mode without a license | export `STRIIM_HOME`, or the four license settings ([RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md), "Docker mode") |
| "not available for download" | `STRIIM_HOME` names a release that is not published | unset `STRIIM_HOME` and export the four license settings, or supply the installers with `SLT_STRIIM_DEPS_MANIFEST` |
| the build refuses: not enough free disk | the first image build needs 35 GB free in Docker | free space (`docker system df`), or raise Docker Desktop's disk |
| "would bring the total number of CPUs to 48 … Beginning Shutdown" | the second node counts every CPU and exceeds the license | `export SLT_STRIIM_PRIMARY_CPUS=12 SLT_STRIIM_NODE_CPUS=12` |
| skipped: `provisioned cluster did not become reachable in time` (exit 3) | the Docker cluster did not start | read `cluster-logs/` in the run directory; check the two rows above |
| `PathConfigError: KEY='…' … does not exist` | a path setting names a folder that is not there | fix it, or unset it to use the default |

## Selecting cases

| You see | Why | Fix |
|---|---|---|
| `... is outside every case root` (exit 2) | the path is not under `SLT_LIVE_CASES` (or your manifest's suites) | move the case, or point the setting at it ([SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md)) |
| `doctor` says `not checked: outside every case root` | the same | the same |
| exit 5, nothing selected | the path or `--case` matched no case | `striim-test list` shows what a run can select |
| `PathConfigError` naming `SLT_LIVE_CASES`: a root inside another, or two roots with the same folder name | case ids must stay unambiguous across several roots | list roots that do not nest, with distinct folder names ([internals/ENGINE.md](internals/ENGINE.md), "Several live case roots") |
| collection refuses a duplicate `name:` | two cases in different roots share a name | rename one: names are unique across every root |

## A `test.yaml` mistake

| You see | Why | Fix |
|---|---|---|
| `unknown manifest key(s) ['timout']` | a typo | the message lists the keys allowed there |
| the test passes but checks less than you meant | an `assert:` tier name that is not a real one (`rowcount:`) is **not** rejected, and never runs | use only the tiers in [TEST-YAML.md](TEST-YAML.md) |
| `exact-block-missing` | a spec has `exact:` but the top-level `exact: {version: 1}` is missing | add the block |
| `undeclared-conversion:amount:Decimal` | a numeric, date or JSON column without a type | add it under `exact.columns` |
| `invalid-value:expected:amount` | a golden cell does not parse as the column's type | fix the cell; write `<null>` for NULL |
| `exact-route-unsupported`, or a `lifecycle:` route refused | `exact:` and `lifecycle:` read Postgres only | on other databases use `data` with `rows:` or `match:`, or `diff`, and no `lifecycle:` |
| a `ddl:` file refused: schema or publication | the framework creates and drops the schemas a test uses | remove the `CREATE SCHEMA` / `CREATE PUBLICATION` |

## During a run

| You see | Why | Fix |
|---|---|---|
| `run` printed `run-dir:` and nothing else | a framework pinned before the result line printed only failures: nothing more means the run passed | `outcome.json` in the run directory says `"exit": 0`; move `framework.pin` to a newer release and run `python3 scripts/sync-framework.py` for the result line |
| `exact data assertion failed`, with `samples.missing` / `samples.extra` | the target differs from the golden | read the two lists: fix the app, or the golden if the golden is wrong |
| a `match:` test passes with a duplicated row | without `exact:`, `match` compares the distinct set of rows as text | add `exact:` (Postgres), or pin the count with `rows: N` |
| lifecycle `deadline` at readiness | the app never delivered the sentinel, or never reached the baseline | check the app reached RUNNING, the replication slot, and the sentinel's `observe` table and `key` |
| lifecycle `terminal-status:HALT` | the app halted | the Striim server log; the reason is in `evidence.json` |
| lifecycle `zero-count` | the source had no rows to count | seed the source; for an empty correct result use the sentinel kinds |
| `witness-not-owned` | a `lifecycle:` table is not one this test's `ddl:` created | create it in `ddl:`, with `${TID}` in its name |
| `collision:table:...` | a table with this run's name already exists, left by something else | drop it; check every name carries `${TID}` |
| `Insufficient Privilege to get Dialect` at `START` | a component's full name (`<namespace>.<name>`) is too long | keep component names to 21 characters |
| `native-uploads-dir-missing` | native mode, and `STRIIM_HOME` is not the running server's own install | point `STRIIM_HOME` at the server's install, on this machine ([TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md)) |
| an `assert.file` with `exact:` refused, or `recover: {mode: kill}` skipped | native mode: no access to the nodes' files or containers | run it in Docker mode ([WHAT-IS-SUPPORTED.md](WHAT-IS-SUPPORTED.md)) |

## Everything skipped

A case skips when something it needs is missing: the cluster did not form, a service's host is not
set (Teradata, ServiceNow), an optional driver is not installed. `striim-test run` reports a run in
which nothing executed as **exit 3, not a pass**.

- The skip reason is in `junit.slt.json` and `evidence.json` (`run.skipReason`).
- **A Striim container that is up is not a server that is up.** A node can shut itself down (its log
  ends `Striim Server Beginning Shutdown`) while `docker ps` still lists it. Read
  `docker logs --tail 20 slt-node`, then stop and start the cluster:
  `(cd scripts/live && ../../.venv/bin/python -m livetest.cli stop striim)` from the checkout that
  started it (the command stops the stack its `SLT_STACK_PREFIX` names), and run again.

## Per service

| You see | Why | Fix |
|---|---|---|
| creating the Postgres replication slot fails | your own Postgres lacks CDC settings | `wal_level=logical`, `wal2json`, `REPLICATION` on the source user |
| a Postgres reset hangs, then fails naming open transactions | a writer from an earlier run is in the middle of a statement on the tables | stop that app. A writer only idle in a transaction is terminated for you, with one line per backend |
| SQL Server: "capture instance already exists" | a CDC table was dropped without disabling CDC | disable CDC before `DROP TABLE` ([SERVICES.md](SERVICES.md), "SQL Server") |
| Oracle CDC captures nothing | the table name is not qualified with the pluggable database, or the reader is not on the container root | `FREEPDB1.<schema>.<table>` and `${ORACLE_CDC_URL}` ([SERVICES.md](SERVICES.md), "Oracle") |
| MySQL: table not found in a parallel run | `${TID}` used as a suffix, or missing from one of the files | `${TID}` first, in the TQL, SQL and `test.yaml` alike |
| Kafka: a standard Avro deserializer cannot read the topic | Striim's registry framing is length-delimited, not Confluent's | [SERVICES.md](SERVICES.md), "Kafka" |
| GCS: an opaque `storage-http` error | the endpoint is a host name | use `${GCS_ENDPOINT}`, which carries an IP |

## Leftovers

- `--keep-resources` keeps the app, tables and slots of a run for you to inspect. `striim-test doctor`
  then reports the leftover and names the command that removes it:
  `python -m livetest.ownership replay <ledger>`.
- To land on a running app with its data already flowed, and check it by hand:
  `SLT_SKIP_VERIFY=1 SLT_KEEP_RESOURCES=1 striim-test run <case>`. It skips every assertion and
  reports the test skipped.
- Stopping containers: [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md), step 8.

## Integration tier

| You see | Why | Fix |
|---|---|---|
| `another integration-test session is already running against this checkout` | one integration run per checkout | wait for it, or use `--parallel` inside one run |
| `NoSuchFileException` under `target/`, or a run that reports one case fewer than expected | you ran Maven in the module while the tier was building it | let the run finish; do not build the module at the same time |
| a case reports a mismatch in event N, section, column | the emitted events differ from `expected/` | the message names the first difference |
