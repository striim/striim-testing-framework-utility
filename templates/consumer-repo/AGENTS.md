# Instructions for AI coding assistants

This repository holds Striim tests that the Striim testing framework runs. The framework is checked
out beside this repository, at `../striim-testing-framework-utility`, at the version in
`framework.pin`. That checkout is read-only: never change it, and report a framework bug as an issue
instead.

Before writing or changing a test, read `../striim-testing-framework-utility/AGENTS.md` and follow
it. It names the guides to read and the rules every test follows. Start from the closest sample in
`../striim-testing-framework-utility/samples/live/`, or from a test already in this repository.

Keep each app under `apps/<name>/`, with its SQL fixtures beside it, and each test under
`tests/live/<name>/`, with its expected result in `expected/`. Derive every expected row from the
input and explain it; never copy expected rows from a run.

Check your work from this repository's root. These deploy nothing:

```
.venv/bin/striim-test list --targets gold-targets.yaml
.venv/bin/striim-test run tests/live/<name> --targets gold-targets.yaml --dry-run
```

Run a live test only when asked: `.venv/bin/striim-test run tests/live/<name> --targets
gold-targets.yaml`. It deploys apps and creates and drops tables. Never put license keys or
passwords in a file other than `.env`, which git ignores.
