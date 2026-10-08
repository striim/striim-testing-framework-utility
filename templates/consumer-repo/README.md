# Your Striim tests

A starter for a repo of your own Striim tests, run by the Striim testing framework at the version
in `framework.pin`. Copy this folder to a new repo, then:

```
python3 scripts/sync-framework.py       # makes .venv, puts the framework next to this repo at the pin
                                        # and installs it, and makes .env from .env.example
.venv/bin/striim-test run --targets gold-targets.yaml
```

Any `python3` can run the sync script. It makes `.venv` with a Python the pinned framework supports
(3.12 or later), finding one such as `python3.12` on your `PATH` when `python3` itself is older, or
use `--python PATH` to choose. Before the run, open `.env` and set where Striim comes from.
`.env` and `.env.example` start with a dot, so `ls` and Finder hide them; `ls -a` shows them.

`tests/live/orders-cdc` is a working test to start from. The framework's guide
`docs/SET-UP-YOUR-OWN-REPO.md` explains each file, how to change the framework version, and how to
run this in CI.

| File | What it is |
|---|---|
| `framework.pin` | the framework version: a release tag, or a full commit |
| `scripts/sync-framework.py` | makes `.venv`, clones or updates the framework checkout to exactly the pin, installs it, and makes `.env` |
| `gold-targets.yaml` | where your tests are |
| `.env.example` | your settings; the sync script copies it to `.env` when there is none |
| `tests/live/` | your tests, one folder each |
| `AGENTS.md` | instructions for an AI coding assistant working in this repository |

## License

This folder is part of the Striim testing framework and is covered by the framework's `LICENSE`:
Elastic License 2.0 (ELv2). Code you copy from it stays under ELv2: keep this notice and give
anyone you share the copy with a copy of that `LICENSE`.
