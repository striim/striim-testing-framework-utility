# Instructions for AI coding assistants

This repository is a test framework for Striim apps, Open Processors and UDFs. A test is a folder
with a `test.yaml`, the app's TQL, its SQL and a hand-written expected result; `striim-test`
deploys the app to a real Striim, compares what it wrote with the expected result, and cleans up.
Read [docs/START-HERE.md](docs/START-HERE.md) first.

## Where things are

| Path | What |
|---|---|
| `samples/live/` | runnable sample tests, simplest first; copy the closest one |
| `samples/code/` | samples that build and load an Open Processor or UDF |
| `templates/consumer-repo/` | a starter for a test repo of one's own |
| `docs/` | the guides; `docs/TEST-YAML.md` is the reference for every key |
| `docs/tql/` | writing, tuning and reviewing TQL; start at `docs/tql/README.md` |
| `skills/tql-authoring/` | the TQL skill, for assistants that load skills |
| `scripts/cli/` | the `striim-test` command |
| `scripts/live/`, `scripts/integration/` | the two test engines, their services and their own tests |
| `docs/internals/` | how the engines work, for changes to the framework |

## Commands

```
striim-test doctor --case <dir>          # checks settings, Striim, services and the case; changes nothing
striim-test list                         # what a run would select; deploys nothing (writes only a run record)
striim-test run <dir> --dry-run          # the selection, without running; deploys nothing (writes only a run record)
striim-test run <dir>                    # deploys apps, creates and drops tables
```

Exit codes: 0 passed, 1 a test failed, 2 a settings or `test.yaml` mistake, 3 something needed was
missing so tests were **skipped** (not a pass), 4 cancelled, 5 nothing selected.

## Writing a test

Follow [docs/WRITING-TESTS.md](docs/WRITING-TESTS.md). The rules:

- Start from the closest sample in `samples/live/`.
- In TQL, SQL and `test.yaml`, use the framework's tokens (`${NS}`, `${APP}`, `${PG_URL}`, the
  service tokens in [docs/SERVICES.md](docs/SERVICES.md)), never a host, port, user, password or
  bare table name.
- Start every object name with `${TID}` (`${TID_ORACLE}` on Oracle), in every file.
- Keep the TQL's `CREATE NAMESPACE ${NS}`, `USE`, `DEPLOY` and `START` lines. Component names stay
  within 21 characters.
- Do not create schemas or publications in `ddl:` files.
- Data a CDC reader must capture is a `seed:` with `when: post_start`.
- On Postgres routes, use `exact:` with every non-text column typed, and a `lifecycle:` block. On
  other databases those are not supported; use `data` with `rows:` and `match:`, or `diff`.
- Use only the `assert:` tiers listed in `docs/TEST-YAML.md`. An unknown tier name is silently
  ignored, so the test checks less than it says.
- `purpose:` is one line naming the one behaviour the test proves.

## Never, unless the user says so

- Write or change an expected result from what a run produced. Goldens are derived by hand from the
  input data and what the app should do; show the derivation.
- Weaken a test to make it pass: remove `exact:` or `lifecycle:`, raise `timeout`, add `xfail:` or
  `disabled:`, delete rows from a golden.
- Set `SLT_INFRA_OWNERSHIP=exclusive`, run `python -m livetest.cli stop all`, or stop or remove
  `slt-*` containers. Other runs on the machine share them.
- Point a `SLT_*_HOST` setting at a database that holds data anyone needs.
- Write license keys, passwords or real customer data into any file.
- Run `pytest` directly instead of `striim-test`: it ignores `.env`.

## Before saying a test is done

1. `striim-test doctor --case <dir>` prints only `[ ok ]` lines.
2. `striim-test run <dir>` exits 0, and you show that exit code.
3. In the run's `evidence.json`, every entry in `data.comparisons` has `equal: true`.

Report a skip (exit 3) as a skip, with its reason.

## Changing the framework itself

Read [CONTRIBUTING.md](CONTRIBUTING.md). Run the hermetic tests of whatever you touch; every YAML
block in a customer doc needs a `<!-- snippet: ... -->` annotation, which `tests/test_doc_snippets.py`
checks against the code.
