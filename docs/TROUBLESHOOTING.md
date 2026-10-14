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
| `exact-route-unsupported`, or a `lifecycle:` route refused | the selected route or projection does not support that check | check the [route matrix](WHAT-IS-SUPPORTED.md#check-routes); exact supports Postgres, both Spanner dialects and Kafka `kafka_record`, plus Docker files. Keep supported exact checks |
| a `ddl:` file refused: schema or publication | the framework creates and drops the schemas a test uses | remove the `CREATE SCHEMA` / `CREATE PUBLICATION` |

## During a run

| You see | Why | Fix |
|---|---|---|
| `run` printed `run-dir:` and nothing else | a framework pinned before the result line printed only failures: nothing more means the run passed | `outcome.json` in the run directory says `"exit": 0`; move `framework.pin` to a newer release and run `python3 scripts/sync-framework.py` for the result line |
| `exact data assertion failed`, with `samples.missing` / `samples.extra` | the target differs from the golden | read the two lists: fix the app, or the golden if the golden is wrong |
| a `match:` test passes with a duplicated row | without `exact:`, `match` compares the distinct set of rows as text | add `exact:` on a supported route; a hand-written row count alone cannot check every value |
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

## Check typed, counted Spanner rows

| Code | Meaning | What to change |
|---|---|---|
| `invalid-declaration` | The exact declaration is malformed or has an unknown key/type. | Use the declared keys and types in TEST-YAML; retain the exact check. |
| `invalid-exact-spec` | The exact declaration is malformed or has an unknown key/type. | Use the declared keys and types in TEST-YAML; retain the exact check. |
| `exact-route-unsupported` | This route/projection cannot return typed exact rows. | Use a supported exact route or the route-appropriate assertion; see the route matrix. |
| `canonical-limit-exceeded` | A read exceeds the declared row or byte budget. | Check unexpected rows/duplicates first; choose a sufficient bounded budget for the hand-derived input. |
| `exact-read-error` | The typed target query failed. | Read the nested driver error, check privileges and target naming, and correct the route. |
| `exact-read-timeout` | The typed query did not finish inside its deadline. | Check target reachability and query blocking; the read cannot qualify the result. |

## Keep JSON token types in payload comparisons

| Code | Meaning | What to change |
|---|---|---|
| `undeclared-column` | Strict JSON contains a column absent from the declaration. | Declare every intended column with its token type, or fix the payload producer. |
| `invalid-json` | The payload is malformed JSON, has duplicate keys/non-finite numbers, or has invalid framing. | Fix the producer; NaN and Infinity must not be emitted as JSON. |
| `invalid-value:actual:<column>` | The observed token/value does not match its declared type. | Keep numeric/boolean/null token types; fix a formatter that quotes numbers, and declare binary encoding explicitly. |
| `evidence-invalid` | The framework could not validate the written evidence envelope. | Treat the run as failed even when assertions passed. Report the framework commit and sanitized evidence/stdout through CONTRIBUTING; do not bypass validation. |

## Check file bytes, hashes and the exact rolled inventory

| Code | Meaning | What to change |
|---|---|---|
| `invalid-raw-file-spec` | The raw inventory declaration is malformed or mixed with content assertions. | Use only path and raw on the spec; declare every expected rolled file, bytes, SHA-256 and records. |
| `raw-file-inventory-mismatch` | The file names present differ from the complete declared inventory. | Check the .00 first-roll name, ${TID} rendering, and empty/hidden extra files; fix writer rollover rather than ignoring extras. |
| `raw-file-mismatch` | An expected file has different bytes, hash or record count. | Check formatter members, encoding, separators and headers against the hand-derived golden. |
| `raw-file-limit-exceeded` | The raw read exceeds max_bytes. | Check unintended output first, then declare an adequate bounded max_bytes. |
| `raw-file-invalid-json` | The bytes cannot be parsed as complete strict JSON objects for raw.format: json. | Fix the emitted JSON record; counts cannot establish byte correctness. |
| `raw-file-route-unsupported` | Raw inventory needs Docker access to owned output files. | Run the case in Docker mode with its owned directory. |
| `raw-file-target-not-owned` | The raw path is outside this attempt's confirmed owned directory. | Use ${OWNED_DIR} and let the framework acquire the exact output scope. |

## Prior-attempt Spanner or Kafka resources

Replay refuses these saved entries because matching endpoints cannot prove an
object's incarnation. Read the failed attempt's ledger for exact table/topic
names and backend binding. Confirm the attempt is abandoned and no active run
owns those objects, then remove only those exact names through the backend's
normal table/topic deletion command. Re-run with a fresh attempt. A broad prefix
drop or replay bypass does not establish ownership.

## Prove no matching data arrived after a sentinel

| Code | Meaning | What to change |
|---|---|---|
| `invalid-nothing-spec` | The absence spec has invalid keys, predicate or timing. | Use the route-appropriate nothing block; content keys and project are refused on data absence specs. |
| `nothing-sentinel-unproven` | No satisfied route sentinel authorizes the bounded absence check. | Declare and deliver the lifecycle sentinel through the route reaching the input before checking absence. |
| `nothing-arrived` | A matching row arrived during the declared absence window. | Inspect the offending row and fix the app/input; do not shorten the window to hide it. |
| `nothing-read-error` | An absence probe failed or missed its read deadline. | Correct route reachability/ownership and inspect the nested error; an unreadable sink is not empty. |
| `witness-unresolvable` | A lifecycle observation cannot be rendered/resolved. | Check sentinel tokens, route, table/path/topic and key declarations against the sink shape. |

## Control overlapping seed transactions and recovery commits

| Code | Meaning | What to change |
|---|---|---|
| `invalid-transaction-seed` | A transaction schedule/observation has invalid keys, references or phase ordering. | Use valid session names and supported operations; observe only declared owned source tables and slots. |
| `invalid-transaction-observe` | A transaction schedule/observation has invalid keys, references or phase ordering. | Use valid session names and supported operations; observe only declared owned source tables and slots. |
| `transaction-control-in-sql` | A transaction SQL file contains transaction-control statements. | Move BEGIN/COMMIT/ROLLBACK/SAVEPOINT operations to transactions; keep the file data SQL only. |
| `transaction-step-error` | A scheduled transaction operation failed. | Read the transaction record for session/op/phase and driver error; check source constraints and held sessions. |
| `transaction-cleanup-error` | A held session could not be rolled back/closed during teardown. | Inspect the session error and ensure the source connection can be closed; cleanup failure is retained. |
| `transaction-observe-not-owned` | A diagnostic table/slot is not confirmed for this attempt and route. | Create/declare it through framework DDL; do not use prior-attempt or foreign resources. |

## Check consistent target snapshots against committed prefixes

| Code | Meaning | What to change |
|---|---|---|
| `invalid-snapshot-spec` | The observer declaration is malformed or outside budgets. | Declare every table and every complete prefix, supported types and positive bounded limits. |
| `snapshot-target-not-owned` | A snapshot table is not owned by this attempt. | Create the target in DDL with ${TID} and use the same route. |
| `snapshot-not-committed-prefix` | A consistent observed state matches no declared committed prefix. | Inspect retained history (prefix null, aggregate SHA-256 and total row count) and the completion key diagnostics; correct transaction visibility or the hand-derived prefixes. |
| `snapshot-table-set-mismatch` | A read/golden does not name exactly the declared tables. | Include each table alias in every complete prefix. |
| `snapshot-target-ambiguous` | Two aliases resolve to the same physical target. | Give each alias a distinct owned target table. |
| `snapshot-duplicate-column` | A table returns duplicate column names. | Use unique column names matching the declaration. |
| `snapshot-undeclared-column` | A snapshot has a column outside the declared set. | Declare the complete intended schema or fix the target schema. |
| `snapshot-limit-exceeded` | The observer exceeds row, byte or history limits. | Inspect excess output and set adequate bounded budgets; history is capped. |
| `snapshot-read-error` | The consistent snapshot could not be read inside its deadline. | Check connectivity, privileges and blocked queries; inspect retained observer error/history. |
| `snapshot-read-timeout` | The consistent snapshot could not be read inside its deadline. | Check connectivity, privileges and blocked queries; inspect retained observer error/history. |
| `snapshot-final-prefix-missing` | The final observed snapshot is not the declared final prefix. | Check completion and full transaction delivery; retain every expected row. |
| `observer-incomplete` | The case ended before the observer could finish qualification. | Read the primary failure and the abandoned observer history; incomplete observation cannot pass. |
| `snapshot-prefix-regressed` | A snapshot matched an earlier declared state after a newer prefix was observed. | Check replay order and transaction preservation; retain the failed history and keep prefixes derived from intended committed states. |

## Reject replay duplicates with an append-only audit

| Code | Meaning | What to change |
|---|---|---|
| `invalid-audit-spec` | An audit declaration is malformed or lacks lifecycle/exact requirements. | Use the audit block and matching exact append target with multiset comparison. |
| `audit-writer-not-append-only` | The declared writer/typed input/target mapping permits upsert or does not match. | Use the route-specific append writer, identical textual target mapping and required false upsert flags. |
| `audit-target-not-append-only` | The target schema/guard permits replacing an existing delivery. | Remove upsert/unique conflicts from the audit sink and retain duplicate rows for comparison. |
| `audit-target-not-owned` | The audit target is not confirmed for this attempt. | Use framework-created ${TID} tables/topics or ${OWNED_DIR} output. |
