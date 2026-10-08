# Upgrading the Java harness

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
