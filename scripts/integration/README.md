# Integration engine (`scripts/integration/`)

The integration tier: `inttest`, which drives an Open Processor's or UDF's code directly, event in
and event out, with no Striim server. `striim-test run --tier integration` drives it.

- Writing integration cases, and the `test.yaml` reference: [docs/INTEGRATION-TESTS.md](../../docs/INTEGRATION-TESTS.md).
- How the engine works, its services, settings and fixtures: [docs/internals/INTEGRATION-ENGINE.md](../../docs/internals/INTEGRATION-ENGINE.md).
- The performance extension: [docs/internals/PERF_SPEC.md](../../docs/internals/PERF_SPEC.md).
