# Upgrading the framework

Review the changes below before reusing services started by an older framework.

## Unreleased

What `main` has changed since v0.3.2. Each heading is something to check before moving your pin
past it. [RELEASE-NOTES.md](../RELEASE-NOTES.md) lists every change.

### Unknown keys in assertion specs (breaking)

A misspelt or unknown key inside an `assert:` spec (`data`, `diff`, `file`, `gcs`, `json`) or in
a `ddl`, `seed`, `server_files`, `generate`, `concurrent` or `stop_start_cycle` entry is now
refused when the manifest loads. Run `striim-test doctor --case <dir>` on your cases before you
move the pin. Each refusal names the file and the key. Correct the spelling (`exact`, not
`exat`); do not delete the key, since it was meant to apply.

### `expect_halt_contains` in Docker mode (breaking)

When a halt happens while the app runs and the cluster's server log cannot be read, the case now
fails instead of passing unchecked. If such a case starts failing with "the node log could not be
read", fix the stack (the app nodes' containers must answer `docker exec`), not the case.

### One rebuild of every operator jar

The live tier now keeps a `<jar>.sources` record as the integration tier does. The first live run
after the upgrade rebuilds each OP and UDF jar once, so allow for the Maven time. After that, a
jar is rebuilt when its source, pom, parent pom, an added source root (`FieldCommon`) or a deleted
file changes, in both engines.

### Data and file `match:` without a count

A data or file `match:` with no `rows:`, `ordered: true`, `events:`, `multiset: true` or
`exact:` now warns at load (`striim-test doctor` lists it) and is refused in a `recover:` case.
Add `rows: N`, counting the golden's rows by hand, or `exact:`. A `rows: 0` spec needs a positive
spec on the same route, a `lifecycle:` block or `expect_halt: true`.

### `assert.json` array order (breaking)

Arrays are compared in order, and `1` no longer equals `1.0`. If a json spec over aggregated child
rows starts failing on order, add `unordered:` with the array paths the failure names. A golden
with two rows for one key is refused.

### Monitor, jmx and gcs specs that only bound

A `monitor`, `jmx` or `gcs` spec with no positive figure (only `max` bounds, zeros, `false`,
`absent`, or an empty object) now holds until `timeout`, so such a case takes its full timeout.
A monitor spec with `{absent: true}` needs a positive figure beside it.

### `project: userdata` goldens (breaking)

A file spec with `project: userdata` now compares events the enricher did not stamp. If your
output includes events with no userdata, the golden needs an all-`<absent>` row, with `keys:`
on the spec.

### Apps left crashed, and teardown that cannot delete (breaking)

A case whose app is in CRASH, HALT, TERMINATED or DEPLOY_FAILED after its last assertion now
fails, smoke-only cases included. A case without `lifecycle:` fails when its teardown cannot
delete what it created. Read the failure; it names the app status or the object.

### Stop cycles and recovery

`stop_start_cycle` waits for each STOP to settle and fails the case if it does not. A `recover:`
STOP or QUIESCE that leaves the app in CRASH or HALT fails the case instead of restarting it.

### Module builds and prebuilt jars (breaking)

A live or integration run in which a case skipped because its module could not be built (no
`mvn`, JDK or `STRIIM_HOME`) now fails, even when other cases passed. A prebuilt jar you name with
`SLT_PREBUILT_*` that cannot be used fails the case in both engines. Install what the reason
names, or leave those cases out of the run.

### Integration `requires:`, `expect_error` and top-level keys (breaking)

An integration case is refused at load when it has a top-level key the loader does not read, a
`requires:` service that no services root defines, or an `expect_error` that renders empty or to
a bare word such as `Exception`. A live `service.yaml` with an unknown key is refused when the
service loads. Run `striim-test doctor --case <dir>`; each refusal names the key or service.

### Perf `target:` iterations

A perf `target:` iteration fails when the writer acknowledged none of the window's events or
published `PROCESSED` 0. A case that measured nothing used to pass.

### Build matrix setting renamed

If you set `FTC_PUBLISHER_RELEASES_ENV`, rename it to `SLT_BUILD_MATRIX_ENV`. The old name is read
only when the new one is unset, and will be removed in a later release.

### Shared integration services stay up

Under `SLT_INFRA_OWNERSHIP=shared`, an integration session no longer takes down the services it
started. If a script relied on that teardown, stop them yourself (`inttest.cli stop`).

### Recovery and action STOPs that leave the app broken (breaking)

A `recover:` with `expect_running: false` now requires the app to settle stopped (CREATED,
DEPLOYED, STOPPED or QUIESCED). A crash or halt during the post-recover settle or the gap between
cycles fails the case, and so does an `alter_recompile` or `drop_recreate_app` STOP or QUIESCE
that crashes a RUNNING app. Read the failure; it names the status.

### `timeout:` and integration load-time refusals (breaking)

A `timeout:` that is not a number (`90s`) is refused at load. An integration case is refused when
it sets `expect_log` beside `expect_error` or on a smoke-only case, and fails when its
`assert.data` input holds no events. `assert.target` rows compare typed. A perf case with a
top-level `timeout:` is refused; move the value to `performance.timeout`. An `INT_VARIANT` that
names no variant is a usage error. Run `striim-test doctor --case <dir>` on your cases.

### `sync-framework.py` inside the framework checkout

The script now refuses to run inside the framework checkout unless `--home` or
`FRAMEWORK_CHECKOUT` names one. Copy the new script from the template into your repo.

### Bare `expect_halt`, refused manifests and xfail outcomes

Set `expect_halt_contains` on every `expect_halt: true` case; the bare form is deprecated and a
later release refuses it. The substring must appear in a log record naming the case's namespace.
A manifest the loader refuses now stops collection (exit 2) even when `-m` would not select it, so
fix or remove it. Tools that read `.slt.json` or `evidence.json` see `xfailed` and `xpassed` where
they saw `skipped` and `passed`.

### `INT_STACK_PREFIX` values

`INT_STACK_PREFIX` must now be lowercase letters, digits and dashes, starting with a letter or
digit, as `SLT_STACK_PREFIX` already had to be. Rename a prefix that is refused.

### Integration item ids (breaking)

A case with variants now runs as one item per variant group, `case[variant]`. Update anything
that selects or reports integration items by id. `INT_VARIANT_ITEMS=0` restores the old ids.

### Integration runs that skip everything (breaking)

An integration run in which every case skipped because the host could not run it exits 1, not 0.
Install what the skip reason names, or set `INT_ALLOW_ALL_SKIPPED=1` if a run of nothing is what
you meant.

### Case records in `.local-evidence/`

Runs write per-case records to `<project>/.local-evidence/case-records`. Add `.local-evidence/`
to your repo's `.gitignore`, or set `SLT_CASE_RECORDS` to another directory or to `off`.

### Diff times

`assert.diff` times now run from the app's start, not the first poll, and a target counted short
is not read in full. Times on large tables are smaller. Do not compare `elapsed_s` across this
upgrade.

### Stopping a shared stack (breaking)

`python -m livetest.cli stop` now exits 2 while another live run uses the stack, and names its
pids. A script that tears an environment down should treat a non-zero exit as "still in use",
wait, and retry; add `--force` only when the named sessions are known to be gone or expendable.
A marker left by an earlier release counts only while its pid still belongs to the process that
wrote it, so stale markers do not block a stop. `livetest.preflight --restart-app-nodes` under
`shared` refuses the same way. While a stop or that restart runs, `--force` included, a live run
that starts on the same Striim endpoint is refused ("a stop or restart of the stack … is in
progress"), naming the stopping pid; start it again once the stop has finished.

### Integration teardown and locks (breaking)

A failed integration teardown now fails the session (exit 1) and keeps the service in
`.int-services-up.json` for the next session to tear down. The session and compose locks moved
from the state dir to `SLT_LOCK_DIR` (default `/tmp/slt-locks`), so two checkouts on one
`INT_STACK_PREFIX` can no longer run sessions side by side; give each its own `INT_STACK_PREFIX`
if they must.

### `SLT_SHARED_PROVISION`

New. `0` makes a shared run fail a case whose service is not running instead of starting it, and
names the start command. Leave it unset (or `1`) to keep today's behaviour.

### Service ports bind locally (breaking for remote clients)

A framework run now publishes every service port on `127.0.0.1`, and on a Linux engine also on
the Docker bridge gateway, so only this machine and its containers reach them. Before, they were
on every interface. If another machine connects to these services (a remote console, a laptop
reaching a cloud host's databases, a Striim on a second host), either forward the ports over SSH
or set `SLT_BIND_ADDRESS` on the host that runs them (`0.0.0.0` for every interface, as before).
Compose files you run with plain `docker compose` and `SLT_BIND_ADDRESS` unset still publish on
every interface. On Linux, a daemon configured with its own `host-gateway-ip` needs
`SLT_BIND_ADDRESS` set to that address.

The new bindings apply only to containers the framework creates. An `exclusive` run starts a stack
of its own, and an integration run without `SLT_INFRA_OWNERSHIP=shared` runs `compose up`, which
recreates a running service whose bindings changed. A kept stack used through
`SLT_INFRA_OWNERSHIP=shared` (live or integration) is reused as it is: neither a run nor
`livetest.cli start` recreates a running container, so its services stay published on every
interface, and its Striim primary keeps the debugger described below, until you recreate them. Upgrading does not
narrow that stack's exposure by itself. Once no run uses it (`livetest.cli stop` refuses while one
does), stop it and start it again on the new version:

```bash
python -m livetest.cli stop all && python -m livetest.cli start all
python -m inttest.cli stop all && python -m inttest.cli start all   # an integration stack, if you keep one
```

Run them with the settings the stack's runs use: `livetest.cli start` declares ownership as a run
does (`SLT_INFRA_OWNERSHIP=shared` with `SLT_KEEP_SERVICES=1` for a kept stack), and a prefixed
stack needs its `SLT_STACK_PREFIX` (live) or `INT_STACK_PREFIX` (integration).
`docker ps --format '{{.Names}} {{.Ports}}'` shows `0.0.0.0:` on a container still published on
every interface.

### JDWP debugger off by default

The Striim primary no longer runs a Java debugger on port 8787. To debug it, set
`SLT_STRIIM_DEBUG=1`; the port is then published on `127.0.0.1` only, so from another machine
forward it over SSH. A second stack no longer needs `SLT_STRIIM_DEBUG_HOST_PORT` unless both debug.
A running cluster that a shared run reuses keeps the old debugger, on every interface: a shared run
never redeploys the cluster, and the reuse check does not compare its image with the new one.
Recreate it as above (`livetest.cli stop striim`, then `start striim`) once no run uses it.

### Striim dependency digests

Third-party files under `scripts/live/services/striim/images/striim/deps/` must now match the
digests in `scripts/live/services/striim/deps.sha256`. A copy that does not match (an old or
partial download) stops the run, naming it; delete it and run again. Each `striim-*.deb` already
there is pinned on the first run after the upgrade, in `deps/.slt-sha256-pins.json`. If Striim
republishes a package under the same name, delete the file and its entry in that record.

### Switches take `1` or `0` (breaking)

An on/off setting set to anything but `1` or `0` now stops the run with exit 2. Search your
`.env`, CI and scripts for values such as `true`, `yes` or `on` and write `1`. A switch set to `0`
is now off: if a script relied on `SLT_KEEP_SERVICES=0`, `SLT_SKIP_VERIFY=0` or a similar value
turning something on, set `1` instead. `striim-test doctor` lists each bad value.

### `INT_SHARED_SERVICES` removed

The setting is no longer read. A session that set it now takes the integration session lock and
tears down the services it started, like any other. Remove it from your `.env` and scripts; give
a second stack its own `INT_STACK_PREFIX`.

## v0.3.2

### Whole-number decimals in integration goldens (breaking)

A whole-number `BigDecimal` is written as a decimal: Oracle `NUMBER(10,2)` 12500.00 is now
`12500.0`, not `12500`. An integration golden that recorded such a value as an integer
(`5001`) needs `5001.0`.

## v0.3.0

### Unknown `assert:` tiers (breaking)

An unknown tier name (`rowcount:`, say) is refused at load in both engines. It used to be
ignored, and the case passed without it. For a row count use `data:` with `rows:`.

### Exit codes and failed builds (breaking)

A run with a failure and a skip exits 1, not 3. A failed OP or UDF build fails the case instead
of skipping it. A prebuilt jar without the expected suffix fails. Update scripts that read the
exit code.

### Integration comparisons are type-strict (breaking)

The integration mock platform matches Striim 5.4.0.6C. An OP that relied on growing `WAEvent`
arrays fails here as it does in production. Integration `assert.data`, perf correctness and
`assert.json` no longer treat `true` as `1` or `5.0` as `5`. Correct goldens that relied on that.
`expect_error` matches only the exception chain the operator threw.

### Assertions that passed on missing data (breaking)

These now fail:

- a missing Kafka topic;
- an exact `assert.file` with no output file;
- an empty `assert.monitor` response;
- golden rows with more cells than the header;
- a `checkpoint_history: empty` that does not hold for the whole `timeout`.

SQL Server and Teradata `min_rows`, and every `rows:`, wait for committed rows.

### OP-loader poison and app-node restarts (breaking)

A shared or undeclared run no longer restarts the app nodes. It fails the case with
`[slt-op-poisoned]` and names `python -m livetest.preflight --restart-app-nodes`. Only an
exclusive run restarts them.

### MySQL accounts on your own instance (breaking)

The framework creates only the configured schemas on your MySQL. Create the accounts and grants
yourself.

### Manifest checks (breaking)

`op.upload` `to:` must be a plain file name. An `action.concurrent` `db:` must be a route of a
`requires:` service. `seed_when: post_start` with `matrix:` is refused.

### Operator jars per release

With several releases of one Striim series in the `STRIIM_BUILD_*` matrix, each release's jar is
`<Op>-<suffix>.jar`. Add `.release-build-*.xml` to your repo's `.gitignore`.

## v0.2.0

### Timed recovery requires RUNNING (breaking)

Before updating a framework pin, check timed `recover.after` cases: COMPLETED or
halted applications now fail `fault-missed` at that boundary. Keep work in flight
at the configured time, or use a bounded progress trigger.

### Tag-aware selection (breaking)

This affects only scripts that run the engine with pytest directly
([internals/ENGINE.md](internals/ENGINE.md)). There, `-k` now matches manifest
tags as well as test names and paths, so cases elsewhere with a matching tag can
join the run; `-m tag_<name>` selects one tag exactly. `striim-test run` takes
neither flag and does not select by tag: it selects by PATH, `--suite` or
`--case` (case name or dir), which are unchanged. Confirm selection with
`--dry-run` before deploying apps.

### Startup halt checks (breaking)

A DEPLOY/START failure with `expect_halt: true` can no longer silently pass
while skipping declared post-start seeds, generated/server files, lifecycle
or assertions. For a deliberate startup refusal, set `expect_halt_contains`
to the expected diagnostic. A match records which work was skipped; without
a match the skipped declarations fail the case. Keep the assertions for a
halt expected after the app starts.

### Oracle observer retention (breaking)

Case-prefixed Oracle observer views and owned logon triggers are removed even
when apps and ordinary tables are kept. Existing cases must not depend on
retaining those prefixed views for inspection. Unprefixed ordinary data-user
views retain their existing cleanup contract. See
[SERVICES.md](SERVICES.md#oracle-observers) for observer ownership and teardown.

### Credential upload failures (breaking)

GCS/Spanner setup now fails immediately when generated credentials cannot
be uploaded to any eligible application node. Check upload permissions and
key availability on every node; updating the pin no longer allows a run to
continue with missing credentials on one node.

### Smoke and seed timing

Smoke settling is one second instead of five. A smoke pass proves RUNNING,
not completion of slow asynchronous initialization. Keep a lifecycle or
condition-based assertion for readiness. A post-start seed with no explicit
`after` waits at least five seconds after RUNNING when neither the reader
nor lifecycle proves readiness; an explicit `after` replaces that floor.

### Integration target delivery

Rebuild the integration harness on upgrade. Events now pass through the
writer's `receive` method under `synchObject`, matching platform locking;
`flush` remains outside the lock. Review custom adapters that assumed event
delivery bypassed that method or ran without the writer lock.

### Optional service changes

Oracle observer routes require the direct grants in
[SERVICES.md](SERVICES.md#oracle-observers) in the data PDB. Existing databases
do not gain them from an image rebuild alone. Timed and node-fault requirements
above apply independently of resource retention.

Postgres fixture deadlines and idle-transaction budgets remain opt-in. Leave
`SLT_PG_FIXTURE_TIMEOUT`, `SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS` and
`INT_PG_IDLE_TRANSACTION_TIMEOUT_MS` unset to preserve the documented default
behaviour. Idle budgets reach new Striim data-role sessions too; allow for
held transactions and recovery. A manifest's `fixture_timeout` overrides the
fixture setting. See [SETTINGS.md](SETTINGS.md).

### Node faults require explicit opt-in

Set `SLT_ENABLE_NODE_FAULTS=1` only for a dedicated disposable node-fault stack.
Without it every `recover.node` case skips before provisioning, including sample
17 in a default samples run. `SLT_RUN_DISABLED` does not bypass this requirement.
The opt-in adds the secondary API compose overlay; normal stacks publish no
secondary API port.

### Publish the secondary API only when it is the survivor

Sample 17 kills the secondary worker and reads the primary API, so it does not
need this upgrade or stack recreation. Apply the following only when an allowed
fault uses the secondary as its API survivor. The bundled primary also hosts
metadata and is refused as a node-fault target.

A Striim stack started before named-node recovery was added does not publish the
second app node's REST API port. Shared mode reuses that stack without changing
its port mappings. With `SLT_ENABLE_NODE_FAULTS=1`, set `SLT_STRIIM_NODE_HTTP_HOST_PORT` (default 9082; choose another
available port if needed), coordinate everyone sharing that exact stack, then
recreate the Striim service with its updated recipe. From the checkout and settings
that own it, after all shared users have finished:

```
(cd scripts/live && ../../.venv/bin/python -m livetest.cli stop striim)
```

This removes that stack's Striim containers and metadata volume. The next Docker
run requiring Striim recreates them with the new port mapping; retained apps and
metadata in that disposable test stack are lost. Do not use `stop all` for this
upgrade. Native Striim does not use this recipe and skips named-node faults.
See [SETTINGS.md](SETTINGS.md) and [17-one-node-failover](../samples/live/17-one-node-failover/README.md).

## Earlier upgrades

### Java harness package names

The integration harness now uses the `com.striim.testing.inttest` package and the
`com.striim.testing` Maven group. Rebuild the harness when updating the framework.

If you invoke its classes directly, replace `com.striim.field.inttest` with
`com.striim.testing.inttest`, including `IntegrationProcessor` and `PerformanceProcessor`.
Update Java imports and Maven coordinates that reference the harness. The Python engine
already launches the new class names.

Copyable samples now use `com.example` packages, Maven groups and JMX domains.
Rebuild copied samples and update their Java imports, TQL function names, manifests and JMX
assertions together. The shared sample source directory is now `SampleCommon`.
The sample-wide strict-property switch is now `-Dcom.example.strictProperties=true`.
Consumer-provided trail decoder sources must use `com.example.ggtrail.TrailDumpHarness`.
