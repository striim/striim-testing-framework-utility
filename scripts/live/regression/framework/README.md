# `framework/` — the harness testing itself

Other tests check an app, an Open Processor or a writer. These test **the live harness**. Each directory is a minimal, runnable test that exercises one
feature of the `test.yaml` schema and is meant to be read and copied from.

Two jobs, deliberately kept apart:

- **Examples (here).** Positive, end-to-end, on a real cluster. They prove a manifest key
  actually reaches the runner and does the thing — that `after` really delays, that `when`
  really orders seeding against DEPLOY and START. Nothing hermetic can prove that.
- **Negatives (not here).** "This must fail" cannot live in a suite where failure means
  failure. Those stay hermetic, in `scripts/live/tests/`, where the assertion tiers are
  already exercised against their failure paths directly — `test_data.py`, `test_diff.py`,
  `test_halt.py`, `test_json_assert.py` and `test_file_assertion.py` carry ~80 such cases
  between them, and `test_manifest*.py` covers every schema rejection. A cluster negative gets
  built only when nothing hermetic can prove the same thing.

## Running

They carry the `framework` tag and the standard `live` marker, so the normal entry point picks
them up with everything else:

```bash
python -m pytest -m live regression        # the whole suite, these included
```

To run just the tier:

```bash
cd scripts/live
export SLT_INFRA_OWNERSHIP=shared SLT_KEEP_SERVICES=1
STRIIM_PASS=striim python -m pytest -m live -k framework
```

Postgres only, by design — it is the cheapest service to provision, and a gallery that needs
five emulators to demonstrate `when:` is a gallery nobody runs. Features that need another
backend (`json` needs Spanner, `gcs` needs the GCS emulator) are listed as exemptions.

One exemption is not about cost. `generate:` has no example of its own here: `ggtrail` is the
only registered generator kind, and its user (`services/ggtrail/ggtrail-cdc-file-diff`) runs it
end to end, decoding every generated operation, so that case is the demonstration. The hook
itself is covered hermetically (`tests/test_generate_hook.py`, 12 cases over placement, phase
filtering, dest rendering and unknown-kind failure).

## What is here

| Directory | Feature |
|---|---|
| `framework-seed-phases` | `when: pre_deploy` and `when: post_start` in one manifest — and why a CDC target only sees the second |
| `framework-seed-after` | `after:` delaying a post_start seed past RUNNING |
| `framework-assert-data` | the `data` tier in all three modes: `rows`, `match`, `keys` |
| `framework-assert-monitor` | the `monitor` tier: what `MON` shows for a target, counts only, polled until the snapshot catches up |
| `framework-expect-halt` | `expect_halt` + `expect_halt_contains` on an app that fails at runtime |
| `framework-disabled` | the `disabled:` quarantine marker, and `SLT_RUN_DISABLED=1` to force it |
| `framework-tokens` | `${NS}` / `${APP}` / service tokens / `${TID}` isolation / a manifest `tokens:` value, across TQL, SQL and assert targets |
| `framework-server-files` | `server_files` at both lifecycle points, plus the `file` assert tier reading the output back |
| `framework-recover` | `recover:` — interrupt a running app and restart it, then assert. The POSITIVE case: a writer committing every second survives a `STOP` with no loss. A defect reproduction does not belong in a gallery of features that work |
| `framework-recover-quiesce` | the same, under `mode: quiesce` — `QUIESCE APPLICATION`, which unlike `STOP` **does** flush. Positive case, and the matched pair for `framework-recover`: identical fixtures, `mode:` the only difference |
| `framework-recover-kill` | the same, under `mode: kill` — SIGKILL the app node containers, restart them, re-authenticate, wait for the cluster. Positive case. Carries `disabled_parallel` because it takes the shared nodes down with it |
| `framework-action` | `action:` — `stop_start_cycle` stops and restarts the app N times while a `concurrent:` SQL script runs on its own thread throughout. `data: rows` proves the script ran (its failures only log); the diff proves its rows survived both stops. Rows written WHILE stopped are not exercised — no assert tier expresses "at least N". The script is rendered with the same token substitution as every other SQL file — it was `str.format`, which is why three databasewriter cases had hardcoded a schema |
| `framework-diff-poll` | `diff_poll` — the `diff` tier's polling interval |
| `framework-xfail` | `xfail:` — a manifest that documents a defect, reported as expected-fail rather than red |

## The gate

`scripts/live/tests/test_framework_coverage.py` fails when a manifest key or assert tier has no
example here and is not on its `EXEMPT` list, and fails again when an `EXEMPT` entry names
something now covered. Add a key to the schema without an example and the hermetic suite says
so. That check is the reason this tier is a regression suite rather than a documentation folder
that drifts two releases behind the code.

The exemption list is the tier's to-do list; shrinking it to empty is what finishing looks like.

## Conventions

- One feature per directory. A test demonstrating three things teaches none of them.
- `purpose:` is the caption — it names the single behaviour on display.
- Comment the manifest generously. These files are read far more than they are run, and the
  comment explaining *why* a phase is chosen is the part a reader came for.
