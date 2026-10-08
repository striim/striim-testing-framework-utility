# Contributing

## How changes get in

This repository is gated: its maintainers review and merge every change. Anyone can open an issue;
a pull request is reviewed by a maintainer, who merges it or asks for changes. Nothing reaches the
default branch without that review.

For security problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

**Found a bug, or missing something?** Open an issue with:

- the framework version (the commit, or the tag your `framework.pin` names);
- what you ran and what you expected;
- the output of `striim-test doctor --case <the case>`;
- from the failing run's directory, `live/stdout.log` and the case's `evidence.json`, with anything
  you cannot share (host names, data) removed.

Teams using the framework in their own test repo use it read-only, pinned to a version
([docs/SET-UP-YOUR-OWN-REPO.md](docs/SET-UP-YOUR-OWN-REPO.md)): report a bug as an issue rather than
patching the checkout.

## Set up

```
git clone https://github.com/striim/striim-testing-framework-utility.git striim-testing-framework-utility
cd striim-testing-framework-utility
bash tools/make-test-venv.sh
```

The helper creates `.venv-test` with the pinned dependencies for both engines and an editable
install of this checkout. It reuses the environment on subsequent runs and refuses an existing
environment that includes system site-packages. It requires a locally installed Python 3.12;
set `PYTHON=/path/to/python3.12` to select one. It prefers `uv` and falls back to Python's `venv`
and `pip`, using public PyPI only. It does not download Python. The helper works with Bash 3.2.

`requirements-test.in` lists runtime dependencies and test tools, including the build tools for
the editable install. `requirements-test.txt` pins Linux x86_64;
`requirements-test.macos-arm64.txt` pins macOS arm64. Each generated header records the exact
`uv pip compile` command to refresh it. The macOS compile requires a binary `pymssql` wheel.
The optional Teradata driver is omitted; tests needing it skip with a reason.

## Tests

The hermetic tests need no Docker and no Striim. Activate the environment so tests that launch
`python3` children use the same packages, then clear external Python paths and disable Docker:

```bash
source .venv-test/bin/activate
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 DOCKER_HOST=unix:///nonexistent
```

Run the suites for whatever you touch. These framework tests use pytest directly; use `striim-test` when running app cases.

```
# the CLI, the docs, the samples' shape, the template
.venv-test/bin/python -m pytest -q -m 'not live and not docker' tests

# the live engine
(cd scripts/live && ../../.venv-test/bin/python -m pytest -q -m 'not live' tests)

# the integration engine
(cd scripts/integration && ../../.venv-test/bin/python -m pytest -q -m 'not integration and not docker' tests)
```

On a Linux test host with user systemd, prefix each Python command with
`systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0` to cap its memory.

The framework's own live tests (`scripts/live/regression/`) need Striim and Docker:
[docs/internals/ENGINE.md](docs/internals/ENGINE.md), "Running it with pytest".

## The docs are tested

- **The catalog stays current.** Run `python tools/doc_catalog.py` after changing a guide's
  title or opening prose, or adding or removing a Markdown file. The hermetic catalog test
  runs `python tools/doc_catalog.py --check`; see [docs/CATALOG.md](docs/CATALOG.md) for scope.
- **Every YAML block in a customer doc is loaded by the code.** `tests/test_doc_snippets.py` reads
  the docs it lists, and each fenced `yaml` block must be preceded by an annotation saying what it
  is: `<!-- snippet: manifest <case>/<file> -->` (a whole `test.yaml`), `fragment` (part of one),
  `file <case>/<file>`, `service <name>`, `compose <name>`, `project`, or `shape <why>` (YAML that
  is not a whole document of any kind). A new customer doc goes on that list.
- **Links resolve**, in the README, `docs/`, `AGENTS.md`, this file and the template.
- **Some text is pinned** to the code it describes (`tests/test_customer_docs.py`): `.env.example`'s
  keys, the quick-start steps and the run line in the README, the CPU-cap and installer-manifest
  settings in `docs/RUN-YOUR-FIRST-TEST.md`.
- **The samples are tested**: `scripts/live/tests/test_live_samples.py` checks each sample's files,
  manifest and that its golden follows from its data. `tests/test_code_samples.py` builds the code
  samples. `tests/test_consumer_template.py` drives the starter template.

Write customer docs task first: what the reader will have at the end, what they need, then numbered
steps with commands that run as written. No history, no plans.

## What changes together

- **A `test.yaml` key or assert tier:** the loader (`scripts/live/livetest/manifest.py`), its
  hermetic tests, `docs/TEST-YAML.md`, and an example in `scripts/live/regression/framework/`.
  `scripts/live/tests/test_framework_coverage.py` fails when a key has no example and is not on its
  exemption list ([regression/framework/README.md](scripts/live/regression/framework/README.md)).
- **A shipped service:** its folder under `scripts/live/services/` (and `scripts/integration/services/`
  for the integration tier), `docs/SERVICES.md`, the status in `docs/WHAT-IS-SUPPORTED.md`, and a
  regression case under `scripts/live/regression/services/`. The keys a `service.yaml` takes are in
  [docs/YOUR-OWN-SERVICES.md](docs/YOUR-OWN-SERVICES.md).
- **A setting:** where the engine reads it, `.env.example` if a user sets it there, and the doc that
  owns it.
- **A sample:** its folder, its README with the run line, the README's samples table, and
  `test_live_samples.py`.

## How the framework works

[docs/internals/](docs/internals/ENGINE.md): the live engine, the integration engine, the
performance extension, and the design notes on ownership, lifecycle and exact comparison.

## Releases

Maintainers cut tagged releases with `tools/release.py`: [docs/internals/RELEASING.md](docs/internals/RELEASING.md).

## Pull requests

- One change per pull request, with its tests and docs.
- Say what changed and why, and which test suites you ran, with their results.
- Commit messages describe the change, in the present tense.
