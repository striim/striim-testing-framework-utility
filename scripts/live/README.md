# Live engine (`scripts/live/`)

The live tier: `livetest`, a pytest plugin that deploys whole Striim apps and checks what they do.
`striim-test run` drives it.

- Writing and running tests: [docs/START-HERE.md](../../docs/START-HERE.md).
- Every `test.yaml` key: [docs/TEST-YAML.md](../../docs/TEST-YAML.md).
- The services in `services/`: [docs/SERVICES.md](../../docs/SERVICES.md).
- How the engine works, running it with pytest, its settings: [docs/internals/ENGINE.md](../../docs/internals/ENGINE.md).
- The framework's own live tests: `regression/` ([regression/framework/README.md](regression/framework/README.md)).
