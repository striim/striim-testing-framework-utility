# Integration engine (`scripts/integration/`, `inttest`)

How the integration tier works inside: its services, isolation, clean-up, fixtures and settings.
Writing integration cases is covered in [../INTEGRATION-TESTS.md](../INTEGRATION-TESTS.md); the
performance extension in [PERF_SPEC.md](PERF_SPEC.md).

## Running the engine's own tests

Create and activate the pinned `.venv-test` environment following
[../../CONTRIBUTING.md](../../CONTRIBUTING.md#tests), including its Python path and Docker settings.

```bash
cd scripts/integration
../../.venv-test/bin/python -m pytest -q -m 'not integration and not docker' tests     # hermetic, no Docker
```

## Architecture

### Services

Each service has its own compose file and network. The stock ones (all images are public):

- **Postgres** (port 15432): Source/target for Postgres-based operators
- **Oracle** (port 11521): Source/target for Oracle-based operators
- **Spanner** (ports 19010-19020): Source/target for Spanner-based operators
- **GCS** (port 14443, overridable via `INT_GCS_HOST_PORT` — the Docker-published port; set
  `INT_GCS_PORT` to the same value too, so the test client looks in the right place): a
  `fake-gcs-server` emulator. One fixed bucket
  (`${GCS_BUCKET}`), per-test isolation by `${TID}`-prefixed object paths rather than a per-test
  bucket. See `scripts/integration/services/gcs/README.md`.

MySQL, SQL Server and Vertica run too (see `docs/SERVICES.md`, "Integration tier"), and
Teradata and ServiceNow are connection-only. No stock service is opt-in: a case that requires one
starts it.

### Token Isolation

Tests are isolated by two token types:

- **`${TID}`**: Test slug (normalized test name)
  - Example: `test_lookup_by_key[cache-hit]` → `test_lookup_by_key_cache_hit`
- **`${TID_ORACLE}`**: Short hash (T + 9 hex chars)
  - Example: → `T0A1B2C3D`

All table names include tokens, allowing multiple tests to run in parallel without conflicts:

- Postgres: `${PG_SOURCE_SCHEMA}.${TID}tablename` (e.g., `qasource.test_lookup_by_keycustomers`)
- Oracle: `${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}TABLENAME` (e.g., `QASOURCE.T0A1B2C3CUSTOMERS`)

### Cleanup

After each test:
- Postgres schemas/tables are dropped automatically via `cleanup_postgres` fixture
- Oracle users/tables are dropped automatically via `cleanup_oracle` fixture
- Spanner tables (both dialect databases) are dropped automatically via
  `cleanup_spanner` fixture, prefixed by this test's `test_id` (Spanner is
  `isolation: none` -- one shared instance/databases -- so there's no per-test
  schema/user the way Postgres/Oracle have; the prefix is what keeps concurrent
  tests' tables apart). The YAML `requires:` pipeline instead drops this test's
  `${TID}`-prefixed tables imperatively (`IntYamlItem._teardown_db_isolation`, since
  a raw `pytest.Item` gets no fixtures); a serial run (where `${TID}` is empty)
  drops nothing there, so Spanner DDL fixtures authored for that pipeline must be
  self-healing.

Cleanup is skipped if a test fails, allowing manual debugging.

## Fixtures

Core fixtures (from `conftest.py`):

- **`tokens`**: Dict of all token values for the test
- **`pg_qasource`**, **`pg_qatarget`**: Postgres source/target connections
- **`ora_qasource`**, **`ora_qatarget`**: Oracle source/target connections
- **`spanner_client`**: raw `google.cloud.spanner.Client`, pointed at the emulator
- **`spanner_admins`**: `{"spanner-google": SpannerAdmin, "spanner-postgres": SpannerAdmin}`,
  instance + both databases ensured
- **`pg_schema`**, **`ora_schema`**: Schema names for this test (includes TID)

## Path settings
Nothing needs setting inside this checkout. The keys, their precedence and the `.env` rules are in
`docs/internals/ENGINE.md` ("Path and connection settings"). The integration engine reads:

| Key | Default |
|---|---|
| `SLT_PROJECT_ROOT` | the repo root (modules named by `jar:`) |
| `SLT_FRAMEWORK_HOME` | `scripts/` (shared with the live engine) |
| `SLT_INT_CASES` | `scripts/integration/regression`; perf cases are its sibling `perf/` |
| `SLT_INT_SERVICES_DIR` | `scripts/integration/services` |
| `SLT_STATE_DIR` | `scripts/integration` (`.int-*` files, `.perf-results/`) |

- `SLT_INT_CASES` is the tree `striim-test` collects, and the plugin reads the same root, so the
  perf root (its sibling `perf/`) follows it. Unset, it is this checkout's
  `scripts/integration/regression`, whatever `SLT_FRAMEWORK_HOME` says.
- `SLT_FRAMEWORK_HOME` moves the Java shim (`integration/java`: the harness jar is built and run
  from there) and the services and state defaults. `inttest` itself still loads from this checkout.
- The Java shim's own tests (`ModuleJars`, run by the shim's `mvn package`) find the modules they
  build under `SLT_PROJECT_ROOT` when it is set, as the case jars are; a module missing there skips
  the test. The value must be absolute (a leading `~/` is expanded). Unset, they walk up from the
  shim's own `integration/java`.

## Environment Variables

Tests read from environment (with fallback defaults):

- `POSTGRES_HOST` (default: localhost)
- `POSTGRES_PORT` (default: 15432)
- `POSTGRES_SOURCE_SCHEMA` (default: qasource)
- `POSTGRES_TARGET_SCHEMA` (default: qatarget)
- `ORACLE_HOST` (default: localhost)
- `ORACLE_PORT` (default: 11521)
- `ORACLE_SOURCE_SCHEMA` (default: QASOURCE)
- `ORACLE_TARGET_SCHEMA` (default: QATARGET)
- `SPANNER_HOST` (default: localhost)
- `SPANNER_GRPC_PORT` (default: 19010)

Those defaults are the *service.yaml* defaults, not constants: each `service.yaml` declares
its published-port variable under `docker_env:` (`INT_PG_HOST_PORT`, `INT_ORA_HOST_PORT`,
`INT_SPANNER_GRPC_HOST_PORT`/`INT_SPANNER_REST_HOST_PORT`, `INT_GCS_HOST_PORT` — the same
variables `compose.yaml` interpolates), so remapping a busy port, or standing a second stack
up from another checkout, moves the container **and** every client that dials it.

Two narrower overrides still win over `docker_env`, and it matters which resolver honors which:
the bare names above (`POSTGRES_PORT`, ...) are read by `inttest/plugin.py`'s fixture path, and
the `live_env` names (`INT_PG_PORT`, `INT_ORA_PORT`, `INT_SPANNER_PORT`, `INT_GCS_PORT`) by
`inttest/tokens.py`, which is what the YAML `requires:` pipeline uses. Setting only one moves
only half the harness — the exact split that made a second stack drive the wrong database
before `docker_env` existed. Reach for either only when the client must dial somewhere other
than where docker published.

GCS has no `conftest.py` fixture (its client is built per-test inside `gcsadmin.py`/`plugin.py` from
the `tokens` dict, not exposed as a fixture) and no bare-name env override — it's overridden the
tokens.py way, via `services/gcs/service.yaml`'s `live_env`: `INT_GCS_HOST` (default: localhost),
`INT_GCS_PORT` (default: 14443), `INT_GCS_PROJECT` (default: test-project), `INT_GCS_BUCKET`
(default: the bucket in `services/gcs/service.yaml`). A `test.yaml`'s `${GCS_ENDPOINT}`/`${GCS_PROJECT}`/`${GCS_BUCKET}`
tokens are what those resolve to (`http://localhost:14443`/`test-project` and the default bucket) — see `scripts/integration/services/gcs/README.md`.

For parallel execution, set `SLT_PARALLEL=1`.

### ⚠ One session at a time

**Two pytest sessions cannot share this checkout, and the harness now refuses the second one.**
In a serial run `${TID}` is the **empty string**, so every session addresses the *same* fixed
table names in the *same* shared schemas — both sessions drop, recreate and truncate each other's
fixtures mid-test. Service teardown compounds it: `ensure_up` registers only what it *started*, so
a session that borrowed an already-running service has it destroyed, volume and all, when the
owner finishes.

The failures this produces name the wrong thing every time — a unique-constraint violation, a
missing table, a missing lookup row, in whichever module happened to be executing.

```
ERROR: another integration-test session is already running against this checkout
       (.int-session.lock).
```

**A dead session cannot strand the next one.** The marker is an OS `fcntl` lock, not the file's
existence — the kernel drops it when the holder dies, so even a `SIGKILL` (no `atexit`, no
`finally`, no `pytest_unconfigure`) frees it. The `.int-session.lock` file is left behind and is
inert; nothing ever consults whether it exists. Verified end to end: a real session `SIGKILL`ed
mid-run leaves the file on disk with the lock free, and the next run proceeds. There is no
"remove the stale lock" step, and there must never be one — see
`test_session_guard_cannot_strand_future_runs_with_a_stale_file`.

Parallelism *within* one session is fine and is what `SLT_PARALLEL=1 -n N` is for — xdist workers
share the controller's session and each test gets a unique `${TID}`. If you genuinely want two
stacks, give the second its own `INT_STACK_PREFIX` so it owns separate containers and state
files. `INT_ALLOW_CONCURRENT_SESSIONS=1` bypasses the check for anyone who has read this and
means it.

`INT_SHARED_SERVICES=1` is for a caller that owns the service containers itself (a runner
that brings them up once and exports their `INT_*` endpoints): the tier then never starts,
stops or tears down a container, and skips this session check, since concurrency is the point.

### ⚠ A tier REBUILDS the operator module — do not build it yourself while one runs

**The integration and perf tiers run `mvn` in the module under test**, because a tier that drove a
stale jar would report green over a real regression (`opartifacts.py` rebuilds whenever anything
under the module's `src/main`, its `pom.xml`, or a shared source folder it compiles in is newer than the
jar). So a tier in flight and a packaging build in another shell are two Maven builds in one
module directory, and one of them deletes `target/` while the other is compiling.

**It breaks in BOTH directions, and neither failure names the cause.**

- *The build loses.* `javac` reports `NoSuchFileException` on `target/classes/*.class` files that
  plainly exist, plus cascade errors that look like real ones (javac unable to read a class file
  it needs to resolve a lambda's target type). Nothing in the output says "another process
  deleted this".
- *The tier loses.* The run reports a short count that reads as a normal result: a perf run
  reporting **10/11 with 1 skipped** rather than an error.

**If a build fails with `NoSuchFileException` under `target/`, or a
tier returns a count one or two short, check for a concurrent Maven before believing either.**

Unlike "One session at a time", this is not enforced by a lock.

## Execution model

`inttest/plugin.py` collects each `test.yaml` (`pytest_collect_file` → `IntYamlFile`/`IntYamlItem`),
`mvn package`s the module jar if it is missing or built against a different Striim release,
provisions/resets any `requires:` services, then runs the shared Java harness
(`java/`, `com.striim.testing.inttest.IntegrationProcessor`) as a subprocess. The harness loads the jar
in a child classloader, reflectively constructs the op's `Processor` via its canonical
`OpenProcessorCommon` constructor — injecting `MockBuiltInFuncs`/`MockTypeResolver` by parameter
type — feeds it each input `WAEvent` through `EventProcessor.processEvent`, and serializes every
emitted event back to JSON for comparison against `expected/`. No live Striim server and no Striim
install on the classpath at run time (only to build the jar).

## Troubleshooting (engine)

**Services won't start:**
- Check Docker is running: `docker ps`
- Check ports are free: `lsof -i :15432` (Postgres), `lsof -i :11521` (Oracle)

**Tests hang on Oracle:**
- Oracle startup can take 1-2 minutes first run
- Check logs: `docker logs slt-oracle` (replace with `int-oracle` for integration)

**Token collision (parallel tests fail):**
- Ensure `SLT_PARALLEL=1` is set for parallel runs
- Without it, pytest will reject `-n` with an error

**Leftover tables after test failure:**
- Manual cleanup: `psql -h localhost -U qasource -d intdb -c "DROP SCHEMA slt_test_name CASCADE"`
- Or use `--slt-keep-resources` to keep schemas for manual inspection
