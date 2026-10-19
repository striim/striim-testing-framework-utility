# Upgrading the framework

Review the changes below before reusing services started by an older framework.

## v0.2.0

### Timed recovery requires RUNNING (breaking)

Before updating a framework pin, check timed `recover.after` cases: COMPLETED or
halted applications now fail `fault-missed` at that boundary. Keep work in flight
at the configured time, or use a bounded progress trigger.

### Tag-aware selection (breaking)

`-k` now matches manifest tags as well as test names and paths. Review scripts
that use it to select a module: cases elsewhere with a matching tag can join
the run. Use explicit case paths for a fixed selection, or `-m tag_<name>`
when a tag is intended. Confirm selection before deploying apps.

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
