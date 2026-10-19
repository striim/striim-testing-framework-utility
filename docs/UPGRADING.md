# Upgrading the framework

Review the changes below before reusing services started by an older framework.

## Timed recovery requires RUNNING (breaking)

Before updating a framework pin, check timed `recover.after` cases: COMPLETED or
halted applications now fail `fault-missed` at that boundary. Keep work in flight
at the configured time, or use a bounded progress trigger.

## Node faults require explicit opt-in

Set `SLT_ENABLE_NODE_FAULTS=1` only for a dedicated disposable node-fault stack.
Without it every `recover.node` case skips before provisioning, including sample
17 in a default samples run. `SLT_RUN_DISABLED` does not bypass this requirement.
The opt-in adds the secondary API compose overlay; normal stacks publish no
secondary API port.

## Publish the secondary API only when it is the survivor

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

## Java harness package names

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
