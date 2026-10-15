# Upgrading the framework

Review the changes below before reusing services started by an older framework.

## Publish the survivor API for named-node recovery

A Striim stack started before named-node recovery was added does not publish the
second app node's REST API port. Shared mode reuses that stack without changing
its port mappings. Set `SLT_STRIIM_NODE_HTTP_HOST_PORT` (default 9082; choose another
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
