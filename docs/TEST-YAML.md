# `test.yaml` schema reference

Authoritative reference for every key a live-test manifest accepts. Source of truth is
`scripts/live/livetest/manifest.py` (`load_manifest`/`_normalize_modules`/`_normalize_file_specs`/
`_normalize_server_files`) plus `scripts/live/livetest/assertions/*.py` for the `assert:` tiers — read
those if this doc and the code ever disagree. [WRITING-TESTS.md](WRITING-TESTS.md) walks through writing
one test from start to finish; this doc is the scannable key-by-key index.

A test is a folder containing a `test.yaml` (this schema) plus whatever it references:
`app.tql`, optional `ddl.sql`/`seed.sql`, an `expected/*.csv` golden per assertion, etc.

## Top-level keys

| key | type | required | default | meaning |
|---|---|---|---|---|
| `name` | string | **yes** | — | the test's name in reports; unique across your tests. Select a test to run by its folder path or `--case`, not by this name |
| `purpose` | string | no* | — | one-line, present-tense description of the single behavior under test (WRITING-TESTS.md, "Rules every test follows"). *Optional at the loader, but every test should have one. |
| `tql` | string | **yes** | — | the app file, relative to `source_dir` (the test dir, or `example:`'s dir if set) |
| `topology` | string | no | `single` | `single` \| `agent` \| `cluster` (`agent` is a legacy alias for `cluster`). Matched against what's resolved; mismatch → **skip**. `single` always runs; `cluster` needs ≥2 nodes in `default` + a registered agent in `Agents` |
| `requires` | list[string] | no | `[]` | service names to provision/reuse (e.g. `postgres`, `oracle`, `mssql`, `kafka`, `spanner`, `gcs`; `teradata` and `servicenow` connect to your own instance, set with `SLT_TERADATA_HOST` / `SLT_SERVICENOW_HOST`) — each resolved once per session |
| `tokens` | mapping | no | `{}` | manifest-declared `${NAME}` values, rendered wherever the harness renders tokens (TQL, DDL/seed, `server_files`, `assert` targets, `kafka_cleanup_topics`, `op.upload` names). Names match `[A-Z][A-Z0-9_]*`; values are strings (numbers are cast) and may not themselves contain `${...}`. A name the harness sets -- `NS`/`APP`/`APP_BARE`/`TID*`, the placement and `STRIIM_*` tokens, a module's `${<TOKEN>_JAR}`/`_NAME`, any service's `provides:` (e.g. `MSSQL_URL`) -- is refused at load, so merge order never decides. Lets one shipped app serve several manifests that differ in a value (e.g. `TrailFilePattern: '${SRC_TRAIL}'`, `k10*` vs `t10*`) |
| `kafka_cleanup_topics` | list[string] | no | `[]` | extra Kafka topics the test's **app** creates that the framework didn't derive itself — e.g. a persisted stream's derived `<ns>_<streamName>` data topic and its `<topic>_CHECKPOINT` companion. Each entry is `${...}`-token-rendered (embed `${NS}`/`${TID}` for per-test uniqueness) and appended to the same best-effort per-test topic delete that already removes the derived `${KAFKA_SRC_TOPIC}`/`${KAFKA_TGT_TOPIC}` at teardown. Requires `kafka` in `requires` (loader fails loud otherwise). Absent/empty (every test that doesn't set it) is a strict no-op |
| `ddl` | string \| list | no | none | schema setup, run once before seed/deploy. Plain string (or a bare list item) → the default `postgres-source` route. List form — entries in the same style `upload:`/`server_files:` use: `- file: <name>` plus an optional `db: <route>` (default `postgres-source`), applied in order. Routes are `<svc>-source`/`<svc>-target` (fixed `qasource`/`qatarget` schemas) plus the emulator routes (`spanner-*`, `gcs`, `kafka`) — see `SERVICES.md`. **Deprecated** (still parses, warns naming the file): the route-keyed entry `{source_db\|target_db: <route>, file: <name>}` — the route value already carries the direction; write `db:` |
| `seed` | string \| list | no | none | data setup, same shape/back-compat as `ddl`, plus two per-entry keys `ddl` does not take: **`when`** (`pre_deploy` (default) \| `post_start` \| `post_recover`) and **`after`** — see below |
| `action` | list | no | `[]` | app lifecycle operations run in order after every `post_start` seed and before every assertion tier. Five types: **`stop_start_cycle`** (`cycles`, `delay_before_stop`, `stop_duration`, optional `concurrent:` SQL loops on background threads); **`drop_recreate_app`** (`app` — token-rendered so a multi-app manifest names just the piece it rebuilds; `delay_before_stop`, `recreate_wait`, `seed`, and `capture`/`stopped_seed`/`tokens`, see [below](#action-drop_recreate_app--rebuild-the-app-from-its-tql)) which STOPs, `DROP APPLICATION … FORCE`s via `StriimClient._force_drop`, then rebuilds the app from its own `CREATE OR REPLACE APPLICATION … END` block in the rendered TQL and waits for RUNNING; **`service_outage`** (see [below](#action-service_outage--take-a-required-service-down-mid-run)) which stops a required service's container mid-run and starts it again; **`capture`** ([below](#action-capture--read-a-value-into-a-token)), which reads a `DESCRIBE` or `MON` field into a token; and **`alter_recompile`** ([below](#action-alter_recompile--replace-components-in-place)), the in-place `ALTER APPLICATION … RECOMPILE` upgrade. Gallery: `framework-action`, `framework-drop-recreate` |
| `assert` | mapping | **yes** | — | at least one tier (`smoke`, `data`, `diff`, `file`, `gcs`, `json`, `monitor`, `checkpoint_history`, `jmx`) — see below |
| `example` | string | no | none | app directory, relative to the project root (`SLT_PROJECT_ROOT`, this clone when unset), with or without an OP/UDF. When set, `tql`/`ddl`/`seed`/`upload` are read from THERE; only `expected/*.csv` and entries marked [`local: true`](#local-true--test-only-files-beside-an-example) stay test-local. Absent → everything is relative to the test dir (self-contained regression style) |
| `op` | mapping \| list | no | none | build + upload OpenProcessor jar(s), registered via `LOAD OPEN PROCESSOR` — single mapping or a list, see below |
| `udf` | mapping \| list | no | none | build + upload UDF jar(s), registered via a global `LOAD` (`client.load_jar`) — single mapping or a list, see below |
| `server_files` | list | no | `[]` | place test-dir files onto the Striim server before deploy or after RUNNING, or upload and register a prebuilt jar (`load:`, from the test dir or outside it) — see below |
| `timeout` | int | no | `120` | seconds an `assert:` tier polls before failing |
| `tags` | list[string] | no | `[]` | free-form labels (e.g. `[op, oracle, regression]`) for `-k`/reporting; not matched against anything in the manifest itself |
| `disabled` | string \| bool | no | — | truthy => the test **skips** before any provisioning (collected + reported as skipped, never run). Use a non-empty string as the reason (e.g. `disabled: "top-level JSON_ARRAY writes {}; issue 123"`). For known-bug / quarantine repros that shouldn't fail CI. Force-run a disabled test with env `SLT_RUN_DISABLED=1` (e.g. to check whether the underlying bug is fixed). |
| `disabled_parallel` | string \| bool | no | — | same shape and skip point as `disabled`, but only takes effect when the run is concurrent (an xdist worker or `SLT_PARALLEL=1`) -- a serial run ignores it entirely. NOT for a test that races or collides with a sibling -- that's an isolation bug, fix the tokenization. NOT for a plain "table name too long" failure either -- shorten the name first (an Oracle table stays under Oracle's identifier-length budget once `${TID_ORACLE}` is prepended; see `_tid_oracle`'s docstring in `scripts/live/livetest/plugin.py`). Only for a defect genuinely triggered by parallel-run token substitution that isn't fixable by renaming, with a linked ticket ref as the reason string. Also force-run-able with `SLT_RUN_DISABLED=1`. |
| `xfail` | string \| dict | no | — | the test is **expected to fail on its assertion** — a committed defect repro that asserts the CORRECT behaviour. String form is the reason; dict form `{reason, strict, tiers, releases}`. `strict: true` (default `false`) turns an unexpected pass into a failure. **`tiers`** names which `assert:` tiers the failure may come from (`data`, `diff`, `file`, `gcs`, `json`, `monitor`, `halt`, `checkpoint_history`, `jmx`; default all of them; **never `smoke`**): only an assertion failure on a named tier is reported XFAIL — a deploy error, a provisioning timeout, an app that never reached RUNNING, a failure on an unnamed tier, or a spec problem is reported as a **real failure**, exactly as it would be without the marker. Before this narrowing an xfail said green for a TQL typo. Run with `--runxfail` to see every outcome as it is. See the section below. |
| `expect_halt` | bool | no | `false` | `true` => the app is expected to reach a terminal status (HALT/CRASH/TERMINATED/DEPLOY_FAILED) instead of RUNNING -- a raise-path test. This can be satisfied at any phase: a synchronous failure during DEPLOY/START itself (the TQL-import call raises before the app is even running), the app never reaching RUNNING during startup, or a later terminal transition while the app is running (polled the same way as any other `assert:` tier). Alone, `expect_halt: true` satisfies the manifest's "at least one assertion" requirement -- no `assert:` block is needed. To also pin *why* it halted, add `expect_halt_contains`. |
| `expect_halt_contains` | string \| list[string] | no | — | narrows `expect_halt` from "any terminal status for any reason" to "a halt whose reason names these substrings". A list means **ALL** substrings must be present (AND), not any-of — pick the one substring that's stable across Striim versions if the phrasing varies. Requires `expect_halt: true` (set alone it would be a silent no-op, and is rejected at load). Where the reason text comes from depends on the phase: a synchronous DEPLOY/START failure matches against the raised import error, which is always available; a **polled** terminal transition matches against the app-node server-log tail, which only exists in **docker** mode — on native single-node (or if the tail read itself fails) the test still PASSES but its record detail says `halt reason not checked (native mode)`, so grep `.slt.json` for that string if you need to know whether the reason was actually verified. Caveat for the polled/docker path: the tail is the **last 20KB of the shared app-node server log**, not scoped to this test's app — under `SLT_PARALLEL` a sibling test's identical error text could produce a false pass, and under heavy log volume the real reason could be evicted past 20KB. Pick a substring unique to your scenario (e.g. the tokenized table/app name), not a generic phrase. |
| `recover` | mapping | no | — | interrupt the app after every `post_start` step and before every assertion, restore it, then assert -- so the assertions describe what SURVIVED the interruption. See **`recover:`** below. Mutually exclusive with `expect_halt` (one expects RUNNING back, the other expects terminal), and requires an assertion beyond `smoke` (which only re-checks RUNNING, something the phase already waits for). |
| `exact` | mapping | no | none | turns on exact comparison for the `data`, `diff` and `file` specs that carry their own `exact:`: every row counted, values compared by declared type. See **`exact:`** below |
| `lifecycle` | mapping | no | none | how the framework knows the app is ready and has finished, before any assertion runs, and that the run cleans up only what it created. See **`lifecycle:`** below |
| `depth` | string | no | `regression` | one of `smoke`, `gate`, `regression`, `fault`, `customer`, `measure`, `canary`; becomes the pytest marker `depth_<value>`, for selecting with `-m` |
| `diff_poll` | number | no | `2` | seconds between the reads of an `assert.diff` poll; must be greater than 0 |
| `generate` | list | no | `[]` | run a data generator and place its output on the Striim server: entries `{kind, workload, dest, when}`. `kind` is `ggtrail` (GoldenGate trail files from a `workload.yaml`); `workload` is relative to the test dir; `dest` is a server directory; `when` defaults to `post_start` |

## `seed:` — `when` and `after`

Each `seed` entry chooses its own lifecycle point, so one test can lay down a baseline before
the app deploys and change data after it is capturing:

<!-- snippet: fragment -->
```yaml
seed:
  - file: seed1.sql
    db: postgres-source
    when: pre_deploy          # default; runs before DEPLOY/START
  - file: seed2.sql
    db: postgres-source
    when: post_start          # runs once the app reaches RUNNING (CDC tests)
    after: 30s                # ...and not until 30s past RUNNING
```

- **`when`** — `pre_deploy` (default) or `post_start`. Per file, so one manifest can use both.
  **`post_recover`** runs after the (last) `recover:` restore reaches RUNNING and before its
  settle, with `after` counted from that RUNNING. It needs `recover:` and is seed-only. Use it
  for data that must arrive after the restart, for example a parent whose child was held
  across the interruption.
- **`after`** — optional delay before this file runs, as `30s`, `500ms`, `2m`, or a bare number
  (seconds). Measured **from the moment the app reached RUNNING**, not from the previous seed
  file, so entries at `10s` and `30s` fire 10s and 30s in, not 10s and 40s. The runner prints a
  heartbeat while waiting so a long delay does not read as a hung test.
- **`after` with `when: pre_deploy` is an error**, not a silent no-op. Pre-deploy seeding runs
  before the app exists, so there is nothing to wait for and an author writing it meant
  `post_start` — the same reason `expect_halt_contains` without `expect_halt` is rejected.
- `ddl` entries take neither key and reject both: schema has to exist before the reader and
  writer start, so it has exactly one lifecycle point.

Reach for `after` when the app needs to settle past RUNNING before data arrives — a reader that
has to establish a change stream or finish a snapshot, for example. It is a blunt instrument: a
fixed sleep makes the test slower and can still be too short on a loaded machine, so prefer an
assertion that waits for a condition where the framework offers one.

## `local: true` — test-only files beside an `example:`

With `example:` set, `ddl`, `seed` and `upload` files come from the example dir. A file only the
test needs (a kill-test config, a recovery seed, extra views) goes in the test dir instead, and
its entry says so:

<!-- snippet: shape local entries; each named file must exist in the test dir -->
```yaml
example: java/OpenProcessors/MyOp/examples/gated
ddl:
  - file: target_pg_ddl.sql            # from the example dir
  - file: test_views.sql               # from the test dir
    db: postgres-target
    local: true
seed:
  - file: source_seed_7.sql
    when: post_recover
    local: true
op:
  jar: java/OpenProcessors/MyOp
  upload:
    - {from: config.json, to: "${NS}-config.json"}
    - {from: config_kill.json, to: "${NS}-kill.json", local: true}
```

- Accepted on mapping-form `ddl` and `seed` entries (any `when`), action `seed`/`stopped_seed`
  entries, and `{from, to}` upload entries. Default `false`.
- A local file is read from the test dir only and a non-local one from the example dir only;
  neither falls back to the other. A missing local file is an error at load.
- Refused: without `example:`; a path that is absolute, contains `..`, or lies under
  `expected/` or `docs/`; the same name marked local in one entry and not in another.
- Local files get the same token rendering and isolation checks as example files.

## `recover:` — interrupt the app, bring it back, then assert

Every other phase verifies a pipeline that ran undisturbed. That cannot observe a whole class of
defect: an app checkpoint claiming progress no target durably made, so the events
in flight at the interruption are never replayed. `recover:` is how a test asks for that.

<!-- snippet: fragment -->
```yaml
recover:
  mode: stop          # required: kill | stop | quiesce
  after: 3            # optional, default 0 -- delay before the FIRST interruption
  times: 2            # optional, default 1 -- how many interrupt/restore cycles
  every: 12           # optional, default = after -- gap between cycles 2..N; needs times > 1
  settle: 30          # optional, default 20 -- pause after the LAST restore, before asserting
  expect_running: true  # optional, default true
assert:               # recover needs an assertion beyond smoke: it says what survived
  diff:
    - source: ${PG_SOURCE_SCHEMA}.${TID}src
      source_db: postgres-source
      target: ${PG_TARGET_SCHEMA}.${TID}tgt
      target_db: postgres-target
```

**`times` / `every` — one interruption often measures nothing.** The first stop usually looks
clean and a divergence shows on the second or third, so `times` repeats the cycle. `after` governs only the first gap and
`every` the rest. **Only the last cycle settles**: settling between cycles would wait out the
very in-flight window the next interruption needs, which is the mistake that makes a recovery
test measure nothing. `every` requires `times > 1`, and `times > 1` requires
`expect_running: true` — otherwise cycles 2..N would "interrupt" an app cycle 1 left stopped and
report success having done nothing. The loader rejects both rather than ignoring them.

**The phase does not judge.** It interrupts, restores, and gets out of the way; `assert.data` /
`assert.diff` say what survived. A recovery test's interesting claim is almost always a
comparison the existing vocabulary already expresses — source vs target, or two fan-out siblings
against each other.

**`mode` — the three are NOT interchangeable, and the difference is the point:**

| mode | What it does | Notes |
|---|---|---|
| `kill` | `docker kill -s KILL` the app node containers, then `docker restart` + re-auth + wait ready | The crash case: no `close()`, no flush, no rollback. **Docker only** — the test SKIPS on a native Striim rather than downgrading to `stop`, which exercises a different path and would report a pass for a case never run. |
| `stop` | `STOP APPLICATION`, then `START` | **Not a drain.** `Flow.stopImpl()` calls `stopDataFlow()`+`stop()` and never `flush()`, so a `DatabaseWriter` discards its pending batch and rolls back its open transaction. This is what field failures are reported on. |
| `quiesce` | `QUIESCE APPLICATION`, then `START` | **The drain.** `Flow.quiesceFlush()` injects a `FlushCommandEvent`, waits for `NODE_APP_QUIESCE_FLUSHED` from every component, then checkpoints. Use as the **control arm**: loss under `stop` and none under `quiesce` localises the fault to the shutdown path rather than the pipeline. |

`stop` and `quiesce` differ by exactly one platform behaviour, which makes them a matched pair —
run the same manifest under both.

> **⚠ `kill` is cluster-wide, not scoped to your app.** It SIGKILLs every container in
> `stack.app_nodes()`, so under `SLT_PARALLEL`/xdist **every other in-flight test on those nodes
> dies with it**. Nothing enforces this today: pair a `kill` manifest with
> `disabled_parallel: true`, or keep it out of concurrent runs. `stop` and `quiesce` are
> app-scoped and safe alongside siblings.

**`after`** is measured from the moment the last `post_start` step finished, not from RUNNING: on
a CDC pipeline nothing is being written until the seed lands, and the point is to interrupt
*while* the app is writing. Accepts the same durations as `seed.after` (`3`, `"500ms"`, `"2m"`),
and `0` is legal — interrupt as soon as the data is in, the widest in-flight window.

**`settle`** is not politeness. Recovery replays asynchronously, so an assertion firing the
instant the app reports RUNNING measures a half-replayed target and reports loss that is only
lag. The status is re-read throughout, so an app that recovers into a HALT is reported as that
rather than discovered later as "no data".

**The app needs a `RECOVERY … INTERVAL` clause.** Without it there is no checkpoint to resume
from and a restarted app simply re-reads from wherever it happens to be — the test would still
pass and would prove nothing.

Example: `scripts/live/regression/framework/framework-recover`, a writer on a tight commit policy
that survives a `STOP`.

## `action: service_outage` — take a required service down mid-run

Stops a service container while the app runs, starts it again, and watches the app ride it out.
It does not judge data; the test's assertions do.

<!-- snippet: fragment -->
```yaml
requires: [postgres]
action:
  - type: service_outage
    service: postgres   # required; must be listed in requires:
    cycles: 1           # optional, default 1
    delay_before: 3     # optional, default 3 -- wait before each stop
    down_for: 5         # optional, default 5 -- how long the service stays down
    signal: KILL        # optional, default KILL -- KILL | TERM | RESTART
    ready_timeout: 120  # optional, default 120 -- wait for healthy after docker start
    settle: 30          # optional, default 30 -- app status watched after each restore
```

| key | meaning |
|---|---|
| `service` | the `requires:` service whose container is stopped (with `RESTART`: whose server restarts in place) |
| `cycles` | stop/start repetitions (with `RESTART`: restarts) |
| `delay_before`, `down_for`, `ready_timeout`, `settle` | durations (`3`, `"500ms"`, `"2m"`); `0` is legal |
| `signal` | `KILL`: `docker kill -s KILL`, no shutdown at all. `TERM`: graceful stop via `docker stop`, which sends the image's stop signal and KILLs after the grace period. The postgres image's stop signal is SIGINT, a fast shutdown that sends `57P01` (admin_shutdown) to every session, so the app sees a different error than under `KILL`. A service whose PID 1 ignores that signal sets `graceful_stop` (see `YOUR-OWN-SERVICES.md`); `TERM` then runs it in the container and waits up to `graceful_stop_timeout` for the container to exit, and fails the test (after a KILL) if it does not. mssql sets it: its PID 1 is a bash wrapper, so `docker stop` would end in SIGKILL. `RESTART`: the container keeps running; the service's `restart_in_place` command (see `YOUR-OWN-SERVICES.md`) restarts the server inside it and returns once it is back, for a server whose container cannot be stopped without harm (the Teradata VM). The manifest fails to load if the service has no `restart_in_place`, and refuses `down_for` and `ready_timeout`. A hook that fails or does not return within `restart_in_place_timeout` fails the test and leaves the container running: it is never KILLed |
| `concurrent` | as for `stop_start_cycle`: SQL loops on background threads for the whole action |

Per cycle: wait `delay_before`, stop the container and confirm it is not running, wait `down_for`,
`docker start` it and wait for its healthcheck to report healthy (5 s after running if it has
none), then re-read the app status for `settle` seconds. An app that leaves RUNNING (COMPLETED is
accepted) fails the test. If a step fails before the restart (including Ctrl-C), the container is
still started before the error is reported. With `signal: RESTART` the stop, `down_for` and
`docker start` are replaced by the one `restart_in_place` call; the settle is the same. A
failure or Ctrl-C during it leaves the container running, and the hook may still be running in
it. A RESTART case needs the service roots that define `restart_in_place` to be active (the
consumer's `servicesRoots`), since the manifest checks for it when it loads.

- **Docker only.** The test skips when the service runs from a live override (e.g. `SLT_PG_HOST`)
  or has no container.
- **It restarts the shared container** (with `RESTART`, the server in it), so every other test
  using that service sees the outage.
  Pair it with `disabled_parallel:`.

## `action: capture` — read a value into a token

Reads one field at this point in the run and binds it to `${<token>}` for everything rendered
after it: a later action's TQL, `stopped_seed`/`seed` SQL, and `assert.monitor` values.

<!-- snippet: fragment -->
```yaml
action:
  - type: capture
    token: RESTART                      # required; a new name, [A-Z][A-Z0-9_]*
    describe: ChangeSource       # DESCRIBE <component> ...
    field: Source Restart Position
  - type: capture
    token: SEEN
    mon: ChangeSource            # ... or MON <component>
    field: input
    timeout: 60s                        # optional, default the manifest timeout
    delay_before: 30s                   # optional, default 0 -- wait before the first read
  - type: capture                       # after an alter_recompile: did the adapter change?
    token: ADAPTER
    describe: ChangeSource
    field: adapterName
    expect: MyCdcReaderV2         # optional: a literal, {present: true} or {matches: <regex>}
```

| key | meaning |
|---|---|
| `token` | the name to set. Refused at load when the harness or `tokens:` already sets it, or when another capture sets it |
| `describe` / `mon` | exactly one: the component to read, token-rendered, qualified by `${NS}` unless it contains a dot |
| `field` | `describe`: every key of that name anywhere in the `DESCRIBE` output (two different values fail) (the restart position sits under `Checkpoint[]`; a `{CheckpointText: …}` value is its text). `mon`: a top-level `MON` figure, as `assert.monitor` reads it. A structured value is captured as JSON |
| `timeout` | how long to poll for a non-empty value (`30`, `"500ms"`, `"2m"`); MON in particular trails the events |
| `delay_before` | a `type: capture` step only: wait this long before the first read, so a value that is still moving (a checkpoint while the events of a `post_start` seed land) has settled. A nested capture runs at its action's STOP and takes none |
| `expect` | optional: the value must equal this literal (token-rendered, compared as text) or satisfy `{present: true}` / `{matches: <regex>}` (a token inside the pattern is regex-escaped). The capture keeps polling until it does, then fails at `timeout` naming the value shown and the one expected. `{absent: true}` is refused: a capture waits for a value; use `assert.monitor` for a missing figure |

- The capture fails when the field never shows a value (naming the fields it did see), and when one
  read shows two different values for it, rather than picking one.
- A token is set only when the capture runs, so a step rendered earlier cannot use it: the first
  deploy of a TQL that names `${RESTART}` fails with `missing token values: RESTART`. To give the
  first deploy a value and the re-created app another, declare a default in `tokens:` and override
  it on the action (`drop_recreate_app` `tokens:`, below).
- To read a value while the app is stopped, use the `capture:` list of `drop_recreate_app` or
  `alter_recompile`, which runs after their STOP. A `type: capture` step reads the app as it is.

## `action: drop_recreate_app` — rebuild the app from its TQL

STOP, `DROP APPLICATION … FORCE`, then re-run the app's own `CREATE OR REPLACE APPLICATION … END
APPLICATION` block from the rendered TQL, its `DEPLOY` and a START. Dropping the app deletes its
checkpoint, so the re-created app starts with none. Three keys act while the app is stopped:

<!-- snippet: fragment -->
```yaml
tokens:
  START_POSITION: ""                     # the first deploy's value
action:
  - type: drop_recreate_app
    app: "${APP}"                        # required, token-rendered
    delay_before_stop: 3                 # optional, default 3
    recreate_wait: 5                     # optional, default 5 -- between the drop and the re-create
    capture:                             # optional: read after STOP, before stopped_seed
      - {token: RESTART, describe: ChangeSource, field: Source Restart Position}
    stopped_seed:                        # optional: runs after STOP and capture, before the drop
      - {file: batch2.sql, db: postgres-source}
    tokens:                              # optional: used only to render the re-created app
      START_POSITION: "^ ${RESTART}"
    seed:                                # optional: runs once the re-created app is RUNNING
      - {file: batch3.sql, db: postgres-source, after: 10s}   # after: counted from that RUNNING
```

- **Order:** STOP (best-effort: a HALTED app still drops); then, when `capture` or `stopped_seed` is
  set, wait until the app has left RUNNING, run the captures and the `stopped_seed` files; UNDEPLOY,
  DROP FORCE, `recreate_wait`, re-create, DEPLOY, START, wait for RUNNING, `seed`.
- **`tokens:`** names must be declared in the top-level `tokens:`, because the first deploy renders
  the same TQL. A value may contain `${...}` (a captured token, typically), rendered when the action
  runs. The override applies to this re-create only.
- `stopped_seed` and `seed` entries take `file` and `db`, like `ddl:`. A `seed` entry also takes
  `after` (`10s`, `500ms`, a bare number of seconds), counted from the moment the action's app was
  RUNNING again, as a top-level seed's `after` counts from RUNNING; `when` is refused. A
  `stopped_seed` entry takes neither.
- Unknown keys are refused.

## `action: alter_recompile` — replace components in place

The documented in-place upgrade. The application is not dropped, so its checkpoint survives and the
re-created components resume from it. The fragment can re-create a source on another adapter (an
older or newer OP version, both loaded with `server_files` `load:`).

<!-- snippet: shape a whole alter_recompile action; `file` names a TQL fragment in the test dir -->
```yaml
action:
  - type: alter_recompile
    app: "${APP}"                        # required, token-rendered
    file: swap_source.tql                # required: the CREATE OR REPLACE statements, token-rendered
    delay_before_stop: 3                 # optional, default 3
    capture:                             # optional: read after STOP
      - {token: RESTART, describe: ChangeSource, field: Source Restart Position}
    stopped_seed:                        # optional: runs after STOP and capture
      - {file: batch2.sql, db: postgres-source}
    seed:                                # optional: runs once the app is RUNNING again
      - {file: batch3.sql, db: postgres-source, after: 10s}   # as for drop_recreate_app
```

`swap_source.tql` holds only what goes between `ALTER APPLICATION` and `RECOMPILE`:

```sql
CREATE OR REPLACE SOURCE ChangeSource USING Global.MyCdcReaderV2 (
  ProjectId: 'test-project', ChangeStreamName: '${TID}cs'
) OUTPUT TO ChangeStream;
```

- **Order:** STOP and wait until the app has left RUNNING; captures; `stopped_seed`; then one import:
  `USE ${NS}; UNDEPLOY APPLICATION <app>; ALTER APPLICATION <app>;` the rendered fragment,
  `ALTER APPLICATION <app> RECOMPILE;`, the TQL's own `DEPLOY APPLICATION <app> …;` (so deployment
  groups carry over; plain `DEPLOY` when the TQL has none) and `START APPLICATION <app>;`. Then wait
  for RUNNING and run `seed`.
- **Refused at load:** a missing or empty `file`, one outside the test dir, one containing a
  statement the action writes itself (`USE`, `ALTER APPLICATION`, `RECOMPILE`, `DEPLOY`/`UNDEPLOY`/
  `START`/`STOP`/`END APPLICATION`, `CREATE APPLICATION`; `--` comments are ignored), and unknown
  keys.
- The fragment is rendered when the action runs, so it can use tokens captured before it.

## `xfail:` — expected to fail, on a named tier only

<!-- snippet: fragment -->
```yaml
xfail:
  reason: "fan-out siblings share one position; a STOP mid-group loses the rest"
  strict: false          # true => an unexpected PASS is a failure (the repro stopped reproducing)
  tiers: [diff]          # only a `diff` assertion failure is the expected one
```

The marker reaches pytest as `pytest.mark.xfail(reason, strict, raises=ExpectedAssertionFailure)`.
The runner re-types an assertion failure as `ExpectedAssertionFailure` only when **every failed
record** came from a tier listed in `tiers`; anything else propagates as itself and the marker,
seeing the wrong exception type, reports a real failure. So the four outcomes are:

| What happened | Reported as |
|---|---|
| an assertion on a listed tier failed | **XFAIL** — the documented defect |
| an assertion on an unlisted tier failed, or `smoke` failed (app not RUNNING) | FAILED |
| deploy / DDL / provisioning / seed raised | FAILED |
| everything passed | XPASS (`strict: false`) or FAILED (`strict: true`) |

`tiers` is a list of `assert:` tier names; `smoke` is rejected at load, as is an unknown key
(so a typo cannot silently widen the marker). The key is `tiers`, not `on`: YAML 1.1 reads a bare
`on` as the boolean `true`. The string form `xfail: "reason"` keeps every tier and `strict: false`.

### `releases:` — only on the named Striim releases

<!-- snippet: fragment -->
```yaml
xfail:
  reason: "Tables newline drops KeyColumns; fixed in 5.4.0.2 and 5.4.0.6G"
  strict: true
  tiers: [json]
  releases: ["5.4.0", "5.4.0.6-5.4.0.6F"]
```

The xfail applies only when the release under test (detected from `STRIIM_HOME`) is listed. On
every other release it is off and the test must pass, including releases nobody has run yet.
An entry is an exact release (`5.4.0` matches `5.4.0` only, not `5.4.0.2`) or a letter range
within one patch line (`5.4.0.6-5.4.0.6F`; the unlettered base sorts before `A`). A range across
lines (`5.4.0.2-5.4.0.6G`) is rejected at load: patch lines ship in parallel, so a fix in one says
nothing about another. List the releases a matrix run actually measured failing.

## `exact:` — compare rows exactly

A plain `match:` compares the **distinct set** of rows, as text: a duplicated row passes, a row that
went missing while a duplicate took its place can pass, and `118.0` is not `118.00`. `exact:` compares
**every row, with its count**, and compares values by their declared type. A missing, extra,
duplicated or changed row fails.

It has two parts: a top-level block that turns it on, and an `exact:` on each spec that uses it.

<!-- snippet: fragment -->
```yaml
exact: {version: 1, max_rows: 10}
assert:
  data:
    - target: "${PG_TARGET_SCHEMA}.${TID}tgt"
      target_db: postgres-target
      match: expected/rows.csv
      exact:
        columns: {id: integer, amount: "decimal:2", created_at: timestamptz}
```

### The top-level block

| key | required | default | allowed |
|---|---|---|---|
| `version` | **yes** | — | `1` |
| `max_rows` | no | `100000` | 1 to 1,000,000: rows read per side |
| `max_bytes` | no | `67108864` (64 MiB) | 1 to 536,870,912: canonical bytes per side |

Nothing is truncated: a side over either limit fails with `canonical-limit-exceeded`. Set the limits
near your data's size, so a runaway target fails fast.

### `exact:` on a spec

`exact: true` takes every default. The mapping form:

| key | default | meaning |
|---|---|---|
| `columns` | `{}` | column name → type (table below). Values of a typed column compare by meaning, so `118.0` and `118.00` are equal as `decimal:2` |
| `ignore` | `[]` | columns removed from both sides before comparing (a load timestamp, say). Each must exist on at least one side, and cannot also be typed |
| `order` | `any` | `any`: the rows compare as a multiset, in any order. `sequence`: also in the same order |
| `order_by` | none | the columns the target is read in order of. Required with `order: sequence` on `data` and `diff`, refused otherwise. The values must be unique per row (`order-not-total` if not) |

The spec's own `keys:` (a projection) still applies, before `ignore`.

**Types.** Every column the database returns as something other than text, a whole number, a
boolean or NULL must be typed, or the run stops with `undeclared-conversion:<column>:<type>`. Text
columns need no entry.

| type | golden cell | compared as |
|---|---|---|
| `integer` | `-12` | the number |
| `decimal:<s>` (s 0 to 38) | `118.00` | the value at `s` places; a value that does not fit exactly is `lossy-decimal`, never rounded |
| `boolean` | `true` or `false` | the value |
| `timestamptz` | ISO 8601 with `Z` or `±hh:mm` (`2026-09-01T08:00:00+00:00`) | the instant, in UTC |
| `timestamp` | ISO 8601 without an offset | the local date and time |
| `date` | `2026-09-01` | the date |
| `binary:hex`, `binary:base64` | the bytes in that encoding | the bytes |
| `json` | JSON text | the JSON, with keys sorted; numbers keep their exact text (`1.0` is not `1.00`) |

**Golden files.** A UTF-8 CSV without a byte-order mark, with a header row of unique names:
- `<null>` is NULL. An empty cell is an empty string, and is refused in a typed column.
- `<absent>` (file specs only) is a column the record does not carry.
- Write the golden by hand from your data. The framework never writes one.

**Where it works.**
- `data` and `diff`: the `postgres-source` and `postgres-target` routes only. Another route is
  refused at load (`exact-route-unsupported`).
- `file`: Docker mode only; the file is read from the cluster's nodes. With `order: sequence` the
  path must match exactly one file on one node (`order-source-ambiguous`).
- With a `lifecycle:` block, every table an exact spec reads (a diff's source too) must be one this
  test's `ddl:` created, and every file must be under `${OWNED_DIR}/` (`exact-target-not-owned`).

**Refused at load** (by `striim-test doctor --case`, and at the start of a run):
- a spec with `exact:` and no top-level block (`exact-block-missing`), or a block with no spec that
  uses it;
- `exact:` without `match:`, or together with `rows`, `min_rows`, `ordered` or `project`;
- an unknown key or type, a column both typed and ignored;
- a `match:` path outside the test's folder.

**Reading the result.** Each comparison is in the run's `evidence.json` under `data.comparisons[]`:
`equal`, and `samples.missing` and `samples.extra`, the rows that differ (up to 20 each, with their
counts). With a `lifecycle:` block the target is read once, after completion. Without one, it is read
again until it matches or `timeout` runs out.

`diff: [{..., exact: true}]` without the top-level block keeps its older meaning: the source and target
rows compared as text, with their counts.

## `lifecycle:` — prove the app finished

Without `lifecycle:`, a test is ready as soon as the app is RUNNING, and done when its assertions
pass before `timeout`. That cannot tell "finished" from "not started yet": an empty or half-copied
target can match too early, and an empty expected result always matches. `lifecycle:` makes the
framework wait for evidence, bounded by deadlines, before any assertion runs.

<!-- snippet: fragment -->
```yaml
lifecycle:
  version: 1
  mode: initial-load
  sink: db
  readiness:
    kind: baseline-landed
    source: {db: postgres-source, table: "${PG_SOURCE_SCHEMA}.${TID}src"}
    target: {db: postgres-target, table: "${PG_TARGET_SCHEMA}.${TID}tgt"}
  completion:
    kind: source-count
    source: {db: postgres-source, table: "${PG_SOURCE_SCHEMA}.${TID}src"}
    target: {db: postgres-target, table: "${PG_TARGET_SCHEMA}.${TID}tgt"}
  stability: 2s
  deadlines: {readiness: 90s, completion: 120s}
  reset: owned
```

### Keys

| key | required | default | allowed |
|---|---|---|---|
| `version` | **yes** | — | `1` |
| `mode` | **yes** | — | `initial-load` (the app reads what exists, then stops producing, or loads a controlled baseline followed by CDC as described in WRITING-TESTS.md) or `cdc` (it captures changes while it runs) |
| `sink` | **yes** | — | `db` (the app writes a table) or `file` (it writes a file) |
| `readiness` | **yes** | — | `{kind: ..., ...}`: when the app is ready for the test's `post_start` data |
| `completion` | **yes** | — | `{kind: ..., ...}`: when the app has finished |
| `stability` | no | `5s` | how long the witnessed value must then stay unchanged; `500ms`, `5s`, `2m` or a number of seconds |
| `deadlines` | no | readiness `60s`, completion the test's `timeout` | `{readiness: ..., completion: ...}`, each greater than 0 |
| `reset` | no | `owned` | `owned` only: the run removes exactly what it created |
| `sentinel` | when a kind is `sentinel` | — | the sentinel's SQL and where to watch it (below); refused when no kind uses it |

### Kinds

| kind | for | keys | satisfied when |
|---|---|---|---|
| readiness `baseline-landed` | `initial-load` | `source: {db, table}`, and `target: {db, table}` (sink `db`) or `path:` (sink `file`) | the app is RUNNING and the target's row count, or the file's line count, equals the source's count taken before deploy |
| readiness `source-progress` | `cdc` | `db: postgres-source` | the replication slot `${PG_SLOT}` is active |
| readiness `sentinel` | `cdc`, sink `db` | none | the ready sentinel was seen on the target, then seen gone |
| completion `source-count` | `initial-load`, sink `db` | `source: {db, table}`, `target: {db, table}` | the target's count equals the source's, and is above 0 |
| completion `row-count` | sink `db` | `db`, `table`, `expect` (above 0) | the table's count equals `expect` |
| completion `file-lines` | sink `file` | `path`, `lines` (above 0) | the file has `lines` lines, counted across its rollover files, blank lines ignored |
| completion `sentinel` | `cdc`, sink `db` | none | the done sentinel, inserted after the `post_start` data, was seen on the target, then seen gone |

Zero is never proof: `expect: 0` and `lines: 0` are refused, and a source baseline of 0 fails the run
with `zero-count`. For a test whose correct result is an empty target, use the `sentinel` kinds
(`samples/live/04-lifecycle-check`).

### The sentinel

<!-- snippet: shape the sentinel block alone; a whole test with it is in WRITING-TESTS.md -->
```yaml
sentinel:
  db: postgres-source                 # where the insert and delete run
  insert: sentinel_insert.sql         # a file in the test folder
  delete: sentinel_delete.sql
  observe:
    db: postgres-target               # where to look for it
    table: "${PG_TARGET_SCHEMA}.${TID}tgt"   # must contain ${TID}
    key: id                           # the column holding the sentinel's id
```

For each phase the framework picks a new id, runs `insert` with it as `${SENTINEL_ID}` (and
`${SENTINEL_TAG}`, a text unique to the run and phase), waits until a row with that id is on the
target, runs `delete`, and waits until it is gone. Only a row the app carried from source to target can
satisfy it. The source table must accept a row that has only the key and the values your `insert`
file gives: fill any `NOT NULL` columns there.

### Rules

- **Routes.** Every `db:` in the block is `postgres-source` or `postgres-target`. A file sink needs
  Docker mode.
- **Paths.** In a test with a `lifecycle:` block, every `assert.file` path, every `server_files`
  `dest`, and the block's own `path`s start with `${OWNED_DIR}/`, with no `.`, `..` or empty parts.
  `${OWNED_DIR}` is a directory on the Striim nodes that the run creates for itself and removes at
  the end.
- **Ownership.** Every table the block reads must be one this test's `ddl:` created, with `${TID}` in
  its name (`witness-not-owned` otherwise), and a target cannot be its own source.
- **Order of a run:** DDL, `pre_deploy` seeds and the source count, deploy, RUNNING, readiness,
  `post_start` seeds, completion, stability, assertions, clean-up.
- **Why a witness failed** is in `evidence.json` under `lifecycle.ready` or
  `lifecycle.completion`: `reason` is `satisfied`, `deadline`, `terminal-status:<status>`,
  `zero-count`, `stability-lost`, `probe-cancelled` or `cancel-failed`, beside the last 50
  observations.

**Clean-up (`reset: owned`).** The run records each object before it creates it, and removes only
those, by exact name: Postgres tables and replication slots, the namespace, and `${OWNED_DIR}`. An
object with the same name that the run did not create is left alone and fails the run
(`collision:<kind>:<name>`). Tables on other databases are not tracked, and are reported as not
verified. With `--keep-resources` nothing is removed; `striim-test doctor` then names the
`python -m livetest.ownership replay <ledger>` command that removes it later.

## Infrastructure ownership (`SLT_INFRA_OWNERSHIP`)

Not a `test.yaml` key, but it decides what a test can assume. Every live run declares it in `.env`
or the shell, and a run without it is refused.

- **`shared`** (with `SLT_KEEP_SERVICES=1`, as `.env.example` has it): the run reuses the Striim
  cluster and the service containers that are already up, starts what is missing, and never tears
  anything down or redeploys the cluster. Other people's tests may be running on the same Striim and
  databases. So every name carries `${TID}` or `${NS}`, and a test never assumes an empty database,
  a fresh cluster or a quiet server log.
- **`exclusive`**: the run starts a stack of its own, refuses to start when anything already answers
  on its Striim endpoint, and tears the stack down at the end unless `SLT_KEEP_SERVICES=1`.

`recover: {mode: kill}` kills every Striim node container, so on a shared cluster it kills the other
tests running there too. Give such a test `disabled_parallel:`, and do not run it on a cluster others
use.

## Unknown keys are rejected

A manifest key the loader does not read is an error naming the key and listing what is allowed.
This catches typos (`timout: 30` never applied its timeout) and keys dropped in a schema change,
both of which used to load silently and take the default — which is how a CDC test ends up
seeding before its reader starts and failing for a reason the manifest does not show.

The same rule already applied inside `op:`/`udf:` entries; it now applies at the top level too.

## `op:` / `udf:` — building/uploading OpenProcessor or UDF jars

Sibling top-level keys, each a single mapping or a list of them; both may be present in
the same test (a TQL app can reference N OPs and M UDFs at once). The KEY itself picks the
load mechanism — no `load:` flag anywhere: an `op:` entry gets `LOAD OPEN PROCESSOR`, a
`udf:` entry gets a global `LOAD` (`client.load_jar`).

**Single-mapping form:**
<!-- snippet: fragment -->
```yaml
op:
  jar: java/OpenProcessors/MyOp              # repo-relative MODULE ref: its dir, or its pom.xml
  upload: [passthrough.json]                # optional; example-relative extra files (e.g. ConfigFile JSON)

udf:
  jar: java/UserDefinedFunctions/MyUdf
```

`upload` entries are either a plain filename (back-compat) or a `{from, to}` mapping:
<!-- snippet: shape the entries of an op/udf upload list -->
```yaml
  upload:
    - passthrough.json            # plain string (back-compat)
    - from: customer_lookup.json  # explicit rename
      to: customer_lookup.json
```
- Plain string — uploaded as `f"{TID}<name>"` (auto per-test-isolated; empty prefix when serial). The test's own TQL must reference that literal `${TID}`-prefixed name.
- `{from, to}` — `from` is the example/test-dir-relative source file; `to` is the uploaded name, token-rendered if it contains any `${...}` (e.g. `${NS}-<name>`), fully author-controlled (no automatic prefix). If the TQL contains a literal `UploadedFiles/<from>` reference (e.g. a shipped example's clean, untokenized `ConfigFile:` line), the runner rewrites it to `UploadedFiles/<rendered to>` before deploy — so a customer-facing example can keep an untokenized `ConfigFile` while the test controls the uploaded name directly. **`to` is not auto-tokenized**: if several tests can run concurrently and upload the same basename (e.g. `products_lookup.json`), give `to` a `${NS}`/`${TID}` prefix to keep them from clobbering each other in the shared `UploadedFiles/`; a bare `to` (matching `from`) is only safe when nothing else uploads that same name at the same time.

A single `op:`/`udf:` mapping normalizes internally to one module with the default token
`OP`/`UDF`, so `${OP_JAR}`/`${OP_NAME}` (or `${UDF_JAR}`/`${UDF_NAME}`) keep working:
`${OP_NAME}` is the built jar's name minus its `-<STRIIM_SERIES>.jar` suffix (e.g. `MyOp`), and
`${OP_JAR}` is the name the jar is uploaded and loaded under.

That name carries a 12-hex tag of the jar's contents (a digest of its entries' names, CRCs and
sizes, not of the file), e.g. `MyOp-1a2b3c4d5e6f-5.4.jar`. A rebuild of unchanged sources keeps
the name, and a name already on the cluster is never overwritten. Striim's OP loader caches a jar
by file name, and a name reused for different bytes fails every later `LOAD` with `ZipFile
invalid LOC header` until the app nodes restart. `${UDF_JAR}` is the built jar's filename.

**List form** — multiple modules under one key, each with its own token:
<!-- snippet: fragment -->
```yaml
op:
  - {jar: java/OpenProcessors/MyMapperOp,  token: MAP}
  - {jar: java/OpenProcessors/MyCleanerOp, token: CLEAN}
udf:
  - {jar: java/UserDefinedFunctions/MyUdf, token: MU, upload: [shared-config.json]}
```
Rules (enforced by `manifest._normalize_modules`):
- `jar` (str, required per entry) — a MODULE reference relative to the project root (`SLT_PROJECT_ROOT`, this clone when unset): its dir, or `pom.xml`, and never a versioned jar path. The framework builds the module in place, writing `<module>/target/`, and may run `mvn clean` there. Must be a real member of the key's family (`op:` → under `java/OpenProcessors/`, `udf:` → under `java/UserDefinedFunctions/`) — a mismatch is rejected loudly at load, not silently mis-driven.
- `token` (str, optional) — defaults to `OP` (for an `op:` entry) or `UDF` (for a `udf:` entry); must match `^[A-Z][A-Z0-9_]*$` when given, and be **unique across the merged `op:`+`udf:` list** (two token-less entries of the same kind collide and raise, naming both). Produces `${<TOKEN>_JAR}` / `${<TOKEN>_NAME}`.
- `upload` (list, optional, per entry) — resolved relative to `source_dir` (the `example:` dir if set, else the test dir); a `{from, to}` entry with `local: true` is read from the test dir.
- No other keys are accepted — an unknown key (including a leftover `load:`) raises at load time.

## `server_files:` — placing files on the Striim server

<!-- snippet: fragment -->
```yaml
server_files:
  - file: fixtures/input.csv     # test-dir-relative
    dest: /tmp/input.csv         # server-side path (docker-cp'd or filesystem-copied, mode-aware)
    when: pre_deploy             # optional; pre_deploy (default) | post_start
```
Each entry needs `file` and `dest`; `when` must be `pre_deploy` or `post_start`.

### `load:` — a prebuilt jar

With `load:`, `dest` is the jar's name, not a path: a `dest` with `/` or `\` is refused at load.
A `load: open_processor` jar is uploaded and loaded under `dest` with a content tag added (see
`${OP_JAR}` above): `MyCdcReaderV1-5.4.jar` becomes `MyCdcReaderV1-<tag>-5.4.jar`, and a name
without the series suffix gets the tag before its first dot (`Dup_5.4.2.scm` becomes
`Dup_5-<tag>.4.2.scm`). Refer to the OP by its `@PropertyTemplate` name, not by its file. The jar is uploaded before
deploy the way `op:`/`udf:` jars are, then registered. Use it for a jar no module in the project
builds, such as an older published release of an OP for an upgrade test.

<!-- snippet: fragment -->
```yaml
server_files:
  - file: ${OLD_JARS}/MyCdcReaderV1-${STRIIM_SERIES}.jar   # outside the test dir
    dest: MyCdcReaderV1-${STRIIM_SERIES}.jar
    load: open_processor
  - file: gs://my-bucket/jars/OtherOp-5.4.jar                     # fetched first
    dest: OtherOp-5.4.jar
    load: op
```

| `load:` | registers with |
|---|---|
| `open_processor` (or `op`) | `LOAD OPEN PROCESSOR`, which registers its `@PropertyTemplate` so `USING Global.<Name>` resolves |
| `udf` (or `true`) | a global `LOAD '<path>'`, unloading first |

- **Source.** A `load:` entry's `file` may be test-dir-relative, absolute, built from `${...}` tokens
  (environment variables and the run's tokens, rendered when the run places it), or a
  `gs://<bucket>/<object>` URL, downloaded with application default credentials. Any other entry
  must be a test-dir file; an outside source is refused at load.
- An unset variable fails the run that needs the jar (`missing token values: OLD_JARS`), not the load
  of the manifest, so a project can carry the case without everyone setting the variable.
- A jar whose bytes differ from a copy already loaded under the same name can break the cluster's
  OP class loader. Give each version its own jar name.

## `assert:` tiers

`assert:` is a mapping of tier name → spec(s). Every tier polls (while the app runs) until
satisfied or the manifest `timeout` expires.

### `smoke`
<!-- snippet: fragment -->
```yaml
assert:
  smoke: true
```
No specs — just asserts the app reaches and stays RUNNING through a short settle window.

### `data` — Postgres/Oracle/MSSQL target row assertions
<!-- snippet: fragment -->
```yaml
assert:
  data:
    - target: ${PG_TARGET_SCHEMA}.hello   # required; ${...}-substituted schema.table
      target_db: postgres-target    # optional; which resolved service's admin to query (default postgres-source)
      min_rows: 1                  # poll until count(*) >= N
      # or: rows: N                # poll until count(*) == N exactly
      # or: match: expected/rows.csv  # distinct row SET == golden CSV (order-/dup-insensitive)
```
Each spec needs `target` plus at least one of `min_rows` / `rows` / `match`. `match` compares
*stringified* values; use `<null>` in the golden CSV for SQL/JSON null. By default it compares
distinct sets; pin `rows: N` too if cardinality matters.

For a Kafka target with JSON message keys and length-delimited Avro values, opt into
`project: kafka_record` to compare the key and value of each record together:

<!-- snippet: fragment -->
```yaml
assert:
  data:
    - target: ${KAFKA_TGT_TOPIC}
      db: kafka
      project: kafka_record
      keys: [key.ID, value.data.ID, value.metadata.OperationName]
      rows: 4
      ordered: true
      match: expected/kafka-records.csv
```

`keys` must contain non-empty dotted paths rooted at `key` or `value`; the CSV uses those
same headers. Missing paths and null values compare as `<null>`; the literal JSON string
`"null"` compares as `null`. Non-JSON key encodings fail explicitly. Each Avro frame is one
record and retains its containing message's key. Existing Kafka value-only assertions keep
their original behavior. The record view rejects tombstones, empty/malformed values and
unread frame bytes. It also fails if the read budget expires before all messages captured
by the initial watermarks are read; a matching prefix cannot satisfy the assertion.
`ordered: true` additionally compares the entire projected list,
including duplicates; use it with a single-partition topic when asserting stream order.
Multiple partitions have no global ordering guarantee. The paired key/value projection
detects swapped or shifted keys even when independent key and value sets would match.

### `diff` — source → target propagation (seed once, assert catch-up)
<!-- snippet: fragment -->
```yaml
assert:
  diff:
    - source: ${PG_SOURCE_SCHEMA}.src   # required
      source_db: postgres-source    # route for the SOURCE endpoint (default postgres-source)
      target: ${PG_TARGET_SCHEMA}.tgt   # required
      target_db: postgres-target    # route for the TARGET endpoint (e.g. spanner-google for a cross-DB diff)
      # (a spec-wide `db:` is still accepted as a fallback for both endpoints)
```
An empty source fails fast (asserts nothing). Polls until the target's distinct row set equals
the source's.

### `file` — `FileWriter`/`JSONFormatter` target assertions
<!-- snippet: fragment -->
```yaml
assert:
  file:
    - path: /tmp/${NS}-out         # required; server-side FileWriter filename, ${...}-substituted
      match: expected/events.csv   # distinct set of projected fields == golden
      # or: min_events: N          # poll until at least N events were written
      # or: events: N              # require exactly N events, including duplicates
      # or: distinct_events: N     # require exactly N distinct projected rows (duplicates ignored)
      project: data                 # optional, default "data"; one of: data | userdata | before | all
      metadata: [OperationName]     # optional; list of metadata keys to fold into the projection
      keys: [ID, NAME]              # optional; restrict the projection to just these column names
```
`events` takes a positive integer and can be combined with `match`, but not with
`min_events`. It counts every parsed event before projection. Use it to catch duplicates
or unwanted UPDATEs that a distinct row-set comparison could hide. Include a terminal
fixture row in `match` when asserting a stream's final count: a count alone cannot prove
that all input has arrived.

`distinct_events` stands alone (not with `match`, `events` or `min_events`). It counts
distinct projected rows (`project`, `metadata` and `keys` apply) and ignores repeats. It is the count for a recovery test whose writer is at-least-once: a
`FileWriter` re-writes whatever followed the app checkpoint after a restart, so `events`
overshoots on a clean run, while a copy that never arrived still lowers `distinct_events`.

Three optional strengtheners, all off by default, so existing cases compare exactly as before:

<!-- snippet: shape keys added to one assert.file spec -->
```yaml
      multiset: true                # the golden is a multiset: duplicates and swaps fail
      order:                        # causal edges, checked in file order
        - before: {OperationName: INSERT, ORDER_ID: '600', ORDER_NAME: held-update-parent}
          after:  {OperationName: INSERT, ORDER_ID: '600', LINE_NO: '1', LINE_TEXT: before-update}
      stable_seconds: 30            # after passing, keep re-reading; a late change fails
```

The edge above uses `project: all` and `metadata: [OperationName]`, as in the held-child UPDATE
case. Each edge is one mapping containing both selectors; two separate list items are rejected.

- `multiset: true` compares the projected events as a multiset against `match`. A duplicate,
  or a missing row swapped for a duplicate at the same total, fails where the distinct-set
  compare passes. It requires `match`.
- `order` is a list of `{before: {...}, after: {...}}` column selectors. Every event matching
  `after` must come later in the file than the first event matching `before`. Events matching
  neither may interleave freely, so unrelated streams are not forced into one order. Each
  selector must match at least one event, so an edge never passes vacuously. Selectors read
  the same projection as `match` (`project` plus `metadata`), not `keys`. File order is the
  writer's append order, which is causal evidence only for one writer on one node.
- `stable_seconds` holds the spec for N more seconds after it first passes. A read that no
  longer passes (a late extra or error event after a matching prefix) fails the run. Use it with
  a terminal fixture row for whole-run completion.

`project` selects which section(s) of each JSON event to flatten before comparing:
- `data` (default) — the row's current-image columns. An event from a typed stream has no
  sections (JSONFormatter writes its fields at the top level); those fields are its data, unless
  the type has a field named `data`, `before`, `userdata` or `metadata`, which reads as a WAEvent.
- `userdata` — what an enricher adds (an OP's lookups, a UDF's stamp).
- `before` — the pre-image (shares column names with `data`; NOT folded into `all` to avoid
  silently overwriting current values — assert it explicitly).
- `all` — `data` + `userdata` merged.

The framework reads the declared file and its numeric rollover siblings — `/tmp/out` →
`/tmp/out.00`, and with an extension the sequence goes before it, split at the name's first dot
(`/tmp/rows.json` → `/tmp/rows.00.json`) — nothing else in the directory. It reads server-side files
mode-aware (docker exec across cluster nodes, or local filesystem for native), and clears prior
output before deploy — embed `${NS}` in the path to keep runs independent.

**Three states, three spellings — `<absent>`, a blank cell, and `<null>`.** A projected row
can be missing a column, carry it as an empty string, or carry it as NULL, and a rectangular
CSV can spell only the middle one on its own. Get this wrong and the failure reads as an
unexplained diff:

| the record | write in the golden |
|---|---|
| does not carry the column | `<absent>` |
| carries it, value empty | a blank cell |
| carries it, value NULL | `<null>` |

**`<null>` — asserting that a column is PRESENT and NULL.** A projected JSON `null` stays
`None`, while a blank cell reads as `''`, so before this marker a null cell could not be
written at all: both spellings were tried against a live run of `csv-format-options` — whose
`nullToken` makes one row's `REGION` null — and both failed.
Unlike `<absent>`, it is accepted under **`assert.data` too**: a SELECT always returns every
column, so `<absent>` can never match there, but it returns NULL as `None`, which is exactly
what this marker produces. It carries the same reserved-value caveat as `<absent>`: a record
whose value is literally the text `<null>` cannot be asserted, and nothing in this repository
needs that literal.

```csv
COUNTRY_NAME,REGION
Deutschland,EMEA
France,<null>
```

**`<absent>` — asserting that a column is MISSING.** `keys` restricts the projection to the
named columns, and a projected row keeps only the keys the record actually carries — so a
record that was not enriched projects to fewer columns than one that was. A CSV row is
rectangular and `csv.DictReader` yields `''` for a blank cell, so a blank cannot mean
"missing": it means "present and empty", and the two must stay distinct. Write `<absent>` as
the cell value and that column is dropped from the expected row, which is what matches a
record lacking it:

```csv
ORDER_ID,PRODUCT_NAME,UNIT_PRICE
5001,Widget Pro,24.99
5005,<absent>,<absent>
```

This is what lets ONE golden cover records of different shapes — an enriched record beside
one a filter, a constraint or `applyTo` excluded. Without it such a case has to fall back to
asserting flags only, which drops the value columns it was covering; that trade was made and
reverted once before the marker existed. `<absent>` is a **reserved value in a golden**, not an escapable one: every cell reading
`<absent>` is stripped, so there is no way to assert that a column's value is literally the
text `<absent>`. Nothing in an event is rewritten — the stripping is applied to golden rows
only — but a record genuinely carrying that text could not be matched, and the failure would
present as an unexplained missing column. No golden in this repository needs the literal.

**Two shapes are refused at load, rather than failing after the assertion timeout.** A golden
whose every row is all-`<absent>` strips to nothing: it is not EMPTY, so the empty-golden
guard cannot see it, but it asserts only "some event carries none of the projected keys" —
which an unenriched record satisfies, so it would stay green through a total enrichment
failure. And an `<absent>` row in a spec with no `keys:` can never match at all, because
without `keys` every compared event projects at least one column. Both raise a
`FileSpecError` naming which of the two problems it is.

`<absent>` is a **`file`-assertion feature only.** It has no meaning under `assert.data`,
which compares against SQL result rows — a `SELECT` always returns every column it names, so
a value is `None`, never an absent key, and the marker could not match. `load_golden` **refuses**
it: writing `<absent>` in a `data:` golden raises a `DataSpecError` at load, naming the
column and pointing here, rather than failing as a value mismatch after the full assertion
timeout.

### `gcs` — GCS object content assertions
<!-- snippet: fragment -->
```yaml
assert:
  gcs:
    - bucket: ${GCS_TGT_BUCKET}     # required
      object: some/path/blob.bin    # required
      sha256: <hex digest>           # one of content_hex / sha256 / size
      # or: content_hex: <hex>       # exact byte-for-byte match
      # or: size: 12345              # byte-length only
```

### `json` — JSON column content assertions (e.g. SpannerWriter JSON columns)
<!-- snippet: fragment -->
```yaml
assert:
  json:
    - target: user_data              # required; table holding the JSON column
      db: spanner-google              # required; which resolved admin to query (needs a
                                       #   JSON-capable admin, e.g. SpannerAdmin)
      key: [user_id]                  # required; key column(s) identifying a row (string or list)
      column: user_responses          # required; the JSON column to compare
      match: expected/user_data.json  # required; golden file, test-dir-relative
```
The golden is a JSON **array of row objects**, each carrying the key column(s) plus the JSON
column, e.g.:
```json
[
  { "user_id": "1", "user_responses": { "ID": "1", "questions": [ /* ... */ ] } },
  { "user_id": "2", "user_responses": { /* ... */ } }
]
```
Reads `SELECT <key…>, <column> FROM <target>` for **all** rows via the resolved db's admin.
Key sets are checked first — a missing/extra key (too few/too many rows) fails immediately.
For each shared key, the JSON column is compared *semantically*: object keys are
order-independent, and arrays are canonicalized (elements recursively sorted by their
canonical serialized form) before comparing, so writer-side array order never causes a
flake. The first differing path is reported, e.g.
`user_data key=('1',): questions[0].answers[0].RESPONSE (expected 'R1', got None)`. Polls
until every key matches or the manifest `timeout` expires.

### `monitor` — what Striim's monitor shows for a component

<!-- snippet: fragment -->
```yaml
assert:
  monitor:
    - target: PgTarget        # the TARGET component; optional when the app has one
      metrics:
        input: 3                       # number: exact
        processed: {min: 1, max: 3}    # bounds: either or both, inclusive
    - component: ChangeSource   # any component: a source, a CQ, an OP
      metrics:
        lastCheckpointedPosition: "^ ${RESTART}"            # a string, with a captured token
        lastEventPosition: {matches: "^\\^ \\{ContinuationToken"}  # a regex (re.search)
        lastErrorPosition: {absent: true}                   # the figure is not shown
        lastSeenPosition: {present: true}                   # shown, with a non-empty value
    - component: ChangeSource   # a value that moves while the app runs
      recapture:                       # re-read on every poll, just before the MON read
        - {token: RESTART, describe: ChangeSource, field: Source Restart Position}
      metrics:
        lastCheckpointedPosition: "^ ${RESTART}"
```

Reads `MON <target>;`, what the web UI's monitor shows, and polls until every figure in `metrics` matches
or `timeout` runs out. A value is compared exactly; `{min, max}` bounds a numeric figure; a mapping
is compared key by key against a structured figure (`individualOperationCount: {Insert: 5}`, or
`{Insert: {min: 1}}`). A mapping with a `min` or `max` key is a bound; refused at load if it has
other keys, a non-numeric bound, or `min > max`. Counts only: rates and clock figures
(`rate`, `lastCommitTime`, `commitLag` and the like) are refused, because they measure the machine.
`target:` and `component:` are exclusive. `component:` names any component in the app's `MON` tree
(a name with a dot is used as is; a name the tree does not list is read as `${NS}.<name>`). Matchers
check a figure's form when its value differs run to run: `{present: true}` (not null, not empty),
`{absent: true}` (missing or null) and `{matches: <regex>}`. Strings, patterns and component names are
token-rendered when the tier runs, so they can carry a token captured earlier; inside `matches` a
token's value is regex-escaped, so `"^\\^ ${RESTART}$"` matches that position literally. A matcher is a
mapping with exactly one of these keys; a pattern is compiled at load unless it carries a token.
`recapture:` (a list of captures: `token`, `describe` or `mon`, `field`) re-reads those values on
every poll, just before that spec's `MON` read, and renders the spec against them. Use it when a value
keeps moving on a running app (a reader's checkpoint advanced by idle heartbeats): a token captured
once is outrun, while a fresh read compares the two figures at one moment. A poll on which a
recaptured field shows no value counts as a miss, not an error. Its tokens follow the capture rules
(no harness or `tokens:` name). It is one read per poll, so `timeout`, `expect` and `delay_before`
are refused, as is a `mon:` recapture of the very figure the spec asserts (it would compare the
figure with itself).
Example: `scripts/live/regression/framework/framework-assert-monitor`.

### `checkpoint_history` — whether Striim recorded a recovery checkpoint

<!-- snippet: fragment -->
```yaml
assert:
  checkpoint_history: nonempty    # or: empty
```

Polls `SHOW <app> CHECKPOINT HISTORY;` until it shows at least one checkpoint (`nonempty`) or none
(`empty`). Use it to tell an app with a `RECOVERY` clause from one without.

### `jmx` — plugin MBean attributes from the Prometheus JMX exporters

<!-- snippet: fragment -->
```yaml
assert:
  jmx:
    - bean: {domain: com.example.cache, type: MyCacheOp, component: ProductEnrich}   # all three required
      attributes:                    # this, rows:, or both
        Hits: 3                      # number: exact
        Misses: {min: 1, max: 2}     # bounds: either or both, inclusive
        GateRunning: false           # boolean: the exporter renders 1.0 / 0.0
      rows:                          # optional: entries of a Map attribute, by key
        HitsByTable:
          "${NS}.${TID}orders": 2      # keys are token-rendered; same value forms
```
Selects `<domain>:type=<type>,name="<NS>.<component>"`. `domain` is the MBean domain your OP
registers its bean under; it has no default, and a spec without it is refused at load.
`component` is the TQL component name, token-rendered and qualified by the test's namespace; a
name containing a dot is used as is. `rows:` asserts entries of an MXBean `Map` attribute, which
the exporter writes one line per entry with the entry's key in a `key` label; each key is
token-rendered and selects its line. A spec needs `attributes:`, `rows:` or both. Scrapes the exporters of the app-group JVMs — `STRIIM_URL`'s host at
`SLT_STRIIM_JMX_HOST_PORT` (default 7071) and `SLT_STRIIM_NODE_JMX_HOST_PORT` (default 7075) —
and polls until every attribute matches or `timeout` expires. Fails if the bean is on no JVM
(listing unreachable endpoints and the exporter lines seen for that domain, such as
`com_example_cache_*`) or on more than one: each JVM's bean counts only its own events, so the tier needs the OP to run on exactly one node. A
deploy that places an instance on every node of a multi-node group (a `cluster` topology) exports
one bean per node and cannot be asserted as one value.
Refused at load: string values (the exporter drops String attributes) and clock attributes, any
name with a CamelCase word such as `Millis`, `Nanos`, `Latency`, `Time`, `Age`. Docker stack
only: on a native Striim the tier fails at once rather than polling. The framework ships no gallery case:
the tier needs an OP that registers such a bean.

**Upgrading a case written before `domain` was required:** add `domain:` to every `bean:` under
`assert.jmx`, set to the ObjectName domain your OP actually registers its MBean under (the part
before the `:`). There is no implicit domain any more; a bean without one is refused at load. Do
not copy the example's `com.example.cache`: a domain other than your OP's selects another bean or
none.

## Substitution tokens (`${...}`)

Every string value (`tql`, `ddl`/`seed` file contents, `assert` targets/paths, `op`/`udf`
refs) is `${TOKEN}`-substituted before use. Tokens come from these places:

- **Framework tokens**: `${NS}` (per-test namespace), `${APP}` (application name). `${NS}` is at most 40
  characters, and a component's full name is `<NS>.<name>`. SpannerWriter fails START with "Insufficient
  Privilege to get Dialect" once that reaches 64, so keep component names at 21 characters or fewer.
- **Manifest tokens**: the `tokens:` key (above) -- per-manifest constants such as a trail
  pattern; never a name the harness itself provides.
- **Captured tokens**: set by an `action: capture` (or an action's `capture:` list) when it runs,
  and used by everything rendered after it.
- **Placement tokens**: `${SOURCE_GROUP}` (= `Agents`, where a `cluster` topology's reader
  runs), `${APP_GROUP}` (= `default`, where the app runs).
- **OP/UDF build tokens**: `${OP_JAR}`/`${OP_NAME}` for a single `op:` mapping,
  `${UDF_JAR}`/`${UDF_NAME}` for a single `udf:` mapping, or `${<TOKEN>_JAR}`/
  `${<TOKEN>_NAME}` per entry in either's list form.
- **Service tokens**: each resolved `requires:` service publishes its own `provides:` map
  from `services/<svc>/service.yaml` — e.g. Postgres gives `${PG_URL}`,
  `${PG_SOURCE_SCHEMA}`/`${PG_TARGET_SCHEMA}`, `${PG_SOURCE_USER}`/`${PG_TARGET_USER}`
  (+ `_PASSWORD`), `${PG_SLOT}` (a per-test replication-slot name); Oracle gives
  `${ORACLE_URL}`, `${ORACLE_CDC_URL}`, `${ORACLE_CDC_USER}`, etc.; MSSQL gives
  `${MSSQL_URL}`, `${MSSQL_HOSTPORT}`, etc. Read the relevant `services/<svc>/service.yaml`
  for the exact token names it emits, and see `SERVICES.md` for the full
  credential/connection model and route-key reference.

## Parallel-safety rules

The suite runs **serially by default** (one shared cluster + service containers);
`SLT_PARALLEL=1` opts in to concurrent runs (`striim-test run --parallel`, or `pytest-xdist -n N`).
For an *author* the flag changes only token values: a serial run renders
`${TID}`/`${TID_UPPER}`/`${TID_ORACLE}` as `""`; a concurrent run renders a short per-test id
with the trailing `_` separator baked in — `${TID}users` → `users` serially, `t1a2b3c4d_users`
in parallel. All three are the same hashed id (`"t"` + 9 hex of a sha256 of the test name),
differing only in case — none of them is a readable test-name slug. Write every test so both renderings work:

- **Every DB object name carries `${TID}`** — tables in ddl/seed/TQL and `assert` targets are
  `${TID}<name>`, and `${TID_ORACLE}<NAME>` for Oracle (unquoted identifiers uppercase; also for
  op-config fields matched against live event metadata). Templates carry no separator of their own.
- **Per-test uploaded files: `upload:` `{from, to}` with a tokenized `to`** — prefix the uploaded
  name `${TID}${NS}_<basename>` (e.g. `to: ${TID}${NS}_products_lookup.json`) so concurrent tests
  sharing a basename never clobber each other in `UploadedFiles/`.
- **Never hardcode bucket/topic/slot/file names.** GCS buckets and Kafka topics are derived per
  test by the framework — reference `${GCS_SRC_BUCKET}`/`${GCS_TGT_BUCKET}` and
  `${KAFKA_SRC_TOPIC}`/`${KAFKA_TGT_TOPIC}`; the Postgres replication slot is `${PG_SLOT}`;
  `assert.file` paths and `server_files` dests embed `${NS}` or `${TID}` (the hermetic
  isolation-enforcement suite fails untokenized paths). Topics the *app itself* creates
  (e.g. a persisted stream's `<ns>_<streamName>`) are already `${NS}`-unique — register them
  in `kafka_cleanup_topics:` so teardown deletes them too.
- **Parallel runs are opt-in and leave teardown to you** — see `docs/internals/ENGINE.md` "Parallel runs
  (`SLT_PARALLEL=1 -n N`)" for the coordination machinery (`SLT_KEEP_SERVICES=1`, one-time
  provisioning, register-once OP jars, no automatic teardown).
