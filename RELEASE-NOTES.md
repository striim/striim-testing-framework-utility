# Release notes

One entry per release, newest first. `## Unreleased` at the top is what `main` has gained since
the last release; the release makes it that version's entry.

## Unreleased

**Breaking changes and changed behaviour**

- **Misspelt or unknown keys in an assertion spec are refused at load.** This covers keys inside
  a `data`, `diff`, `file`, `gcs` or `json` spec, and inside a `ddl`, `seed`, `server_files`,
  `generate`, `concurrent` or `stop_start_cycle` entry. An incomplete `data`, `diff`, `file`,
  `gcs` or `json` spec is refused too, including a typed `exact:` spec without the endpoint its
  reader needs, and `nothing:` is accepted only on `data` and `file`. Before, such a key was ignored, so `exat: true` silently
  ran a weaker comparison, or the case was refused only after deploy. `striim-test doctor`
  exits 2 on these.
- **`expect_halt_contains` fails in Docker mode when the halt reason cannot be checked.** When a
  halt happens while the app runs, the reason is read from the cluster's server log. If that log
  cannot be read, or holds nothing since the case deployed, the case now fails and says why.
  Before, it passed with `halt reason not checked (native mode)`. This failure is not an
  assertion record, so an `xfail` on the `halt` tier does not absorb it. Native mode is
  unchanged: the case passes without the check, and its record says so.
- **Operator and UDF jars are rebuilt when anything they are built from changes, in both
  engines.** That is: the module's source, its `pom.xml`, the parent pom it declares, every
  source root the pom adds (`FieldCommon`, `OpenProcessorCommon`), or a deleted source file.
  Before, the integration and perf tiers reused a jar after a `FieldCommon` edit, and neither
  engine noticed a deleted file or a parent-pom edit. Both engines now keep the same
  `<jar>.sources` record, so a jar built by one is reused by the other. **The first live run
  after upgrading rebuilds each jar once**, because no live jar has a record yet. The integration
  tier rebuilds each `FieldCommon` module once. The integration and perf tiers no longer rebuild
  a module whose pom does not add `OpenProcessorCommon` when that changes. The record holds the
  digests the build started from, so a source saved while Maven runs still counts as changed.
- **A data or file `match:` without a count warns at load, and is refused in a `recover:`
  case.** A `match:` without `rows:`, `ordered: true`, `events:`, `multiset: true` or `exact:`
  compares sets of rows, so a duplicated row passes. `striim-test doctor` shows the warning. Add
  `rows: N` (the golden's row count) or `exact:`. `rows: 0` is refused unless the same route has
  a positive spec, or the case has `lifecycle:` or `expect_halt: true`.
- **`assert.json` compares arrays in order, and integers and floats are distinct.** List the
  arrays whose order the writer does not fix under the new `unordered:` key (`items`,
  `"items[].subitems"`, `$`). `1` no longer equals `1.0`. A golden with two rows for one key, or
  an object with a duplicate member, is refused; a read with two rows for one key fails.
- **`assert.monitor`, `assert.jmx` and `assert.gcs` specs with no positive figure hold to the
  timeout.** A spec with only `max` bounds, zeros, `false`, `absent` or an empty object must keep
  matching until `timeout` instead of passing on the first read. `{absent: true}` needs a
  positive figure in the same monitor spec.
- **A file `match` or `distinct_events` compares every event except a true boundary event** (no
  `data`, `before` or `userdata`). Under `project: userdata`, an event the enricher did not stamp
  now fails the golden instead of being skipped. `project: before` still skips events with no
  before-image. A file whose last event never decodes, or that holds JSON values that are not
  objects, now fails at the timeout instead of passing.
- **Every app's status is read once after the last assertion.** An app in CRASH, HALT,
  TERMINATED or DEPLOY_FAILED fails the case, smoke-only cases included; `expect_halt` cases are
  not read. Lifecycle stability fails with `terminal-status:<status>` when an app has crashed or
  halted by the end of the interval.
- **Teardown failures fail a case without a `lifecycle:` block.** That is: a non-Postgres `ddl:`
  table, a GCS bucket, a Kafka topic, an `op.upload` file or a FileWriter output part that cannot
  be deleted (`unowned-teardown-failed`), or an OP checkpoint that cannot be deleted
  (`op-checkpoint`). These were reported as verification gaps only.
- **`checkpoint_history: empty` no longer passes on a history Striim could not show.** Only "not
  available yet" counts as no checkpoint; any other answer fails `empty` at once, and `nonempty`
  names it at the deadline.
- **`stop_start_cycle` waits for each STOP to settle** (DEPLOYED, STOPPED or CREATED) and fails
  the case otherwise. A `recover:` STOP or QUIESCE that leaves the app in CRASH or HALT fails the
  case instead of restarting it.
- **Live: a case skipped because its module could not be built fails the run**, even when other
  cases passed. A prebuilt jar named by `SLT_PREBUILT_JAR`, `SLT_PREBUILT_JAR_<MODULE>` or
  `SLT_PREBUILT_DIR` that cannot be used fails the case instead of skipping it. A case skipped
  because a service is unavailable counts as absent infrastructure, as a missing Docker does.
- **Integration: a case that could not be tested no longer passes the run.** A `requires:`
  service that no services root defines is refused at load. A module reference whose directory is
  missing, or a prebuilt jar (`SLT_PREBUILT_*`) that cannot be used, fails the case. A case
  skipped because its module could not be built (no `STRIIM_HOME`, `mvn` or JDK) now fails the
  run even when other cases passed, and `INT_ALLOW_ALL_SKIPPED` does not cover it.
- **Integration: `assert.expect_error` is checked after rendering.** A variant that leaves a token
  it uses empty, or renders it to a bare word like `Exception`, is refused at load.
- **Integration: `ignore_fields` on a `metadata`/`userdata` key keeps the presence check.** The
  key must be on exactly the events where the fixture has it.
- **Perf: a `target:` iteration needs evidence of a write.** It fails when the writer
  acknowledged none of the window's events, published `PROCESSED` 0, or reports neither. The
  result file gains `measuredAckedEvents` and `publishedProcessed`.
- **Integration `test.yaml` top-level keys and live `service.yaml` keys are checked at load.** An
  integration case with a top-level key the loader does not read (a misspelt `matrix:`,
  `requires:`, `timeout:` or `disabled:`) is refused, naming the key; it used to load and run
  without it. A live `service.yaml` with a key the service loader does not read (a misspelt
  `live_override_env:`) is refused when the service loads. The perf loader already refused
  unknown top-level keys and now shares the regression loader's key set; it refuses `setup:`,
  which it does not read.
- **`SLT_INFRA_OWNERSHIP=shared` keeps the integration services a session started.** The session
  no longer runs `docker compose down -v` on them at its end; it clears the started-services record
  and prints what it left running, as `INT_KEEP_SERVICES=1` does, which is no longer needed beside
  it.
- **Interrupted and failed bring-ups clean up.** Ctrl-C during a service or cluster bring-up no
  longer leaves containers behind. An exclusive run that is interrupted or fails, serial included,
  removes every container of its prefix. An exclusive declaration warns about containers of
  earlier exclusive runs that nothing owns, with the command to stop them. It is refused when its
  check for leftover containers of its own prefix fails or times out, instead of going ahead.
- **Every Docker command has a bound:** 60 s for queries, 30 min for a copy of a `server_files`
  file or an OP jar and for a read of a FileWriter output (the file assertion's polling read keeps
  60 s, so one hung read does not hold a poll for 30 min), 10 min for `compose down`, 3 h for
  `compose up`, 1 h for captured builds. A hung Docker daemon fails the step and says so instead of
  hanging. Waits for another process's bring-up lock are bounded too, and name the lock. A command
  that outlives its bound, or is interrupted, is stopped with every process it started (the Compose
  plugin under `docker`, a post-up script's or a build's own children, those whose parent has
  already exited included), so no part of a timed-out `compose down` or bring-up goes on after the
  step has failed. Each command runs in a process group of its own, which is also killed when the
  process that ran it dies (a tier that `striim-test` cancels, stops or kills takes its commands with
  it); a signal sent to the caller's process group reaches the caller, whose interrupt stops the
  command. A command that finishes takes with it any helper it left running in its group. The bound
  of a command whose output streams to the terminal holds however fast its output is read; what is
  still to be shown when it is stopped is shown for at most 2 s, then a line says the rest was dropped.
- **recover: `expect_running: false` is checked.** The app must settle CREATED, DEPLOYED, STOPPED
  or QUIESCED after the restore; RUNNING or a terminal status fails the case, and a `recovery`
  record holds the status.
- **recover: the settle after `post_recover` inputs and the `every` gap between cycles watch the
  app**, as the restore's own settle does; the settle's last read is checked too.
- **`alter_recompile` and `drop_recreate_app` (with `capture` or `stopped_seed`):** a STOP or
  QUIESCE that leaves a RUNNING app in CRASH or HALT fails the case before captures, stopped seeds
  or the ALTER run.
- **`test.yaml`: a `timeout:` that is not a number** (`timeout: 90s`, or an empty `timeout`) is
  refused at load as a `test.yaml` mistake naming the file (exit 2), instead of an internal error.
- **Integration: `assert.expect_log` is refused where it is never read**: beside
  `assert.expect_error` (it was a warning, #82), and on a smoke-only case, which never drives the
  operator.
- **Integration: an `assert.data` input with no events fails the case.** The operator was never
  fed, and with `match: []` the case passed. An empty `match` beside real input still says the
  operator filters everything.
- **Integration: `assert.target` rows compare typed**, as `assert.data` values do: `true` is not
  `1`, and `5.0` is not `5`. `ADAPTER_BASIC` subset expectations mean what they say: an empty
  mapping requires an empty object, `null` requires the key to be absent, and list elements
  compare typed.
- **Integration: an `INT_VARIANT` that names no variant of any collected case is a usage error.**
  Before, it dropped those cases and exited 0. A matching name prints the narrowing.
- **Perf: a top-level `timeout:` is refused**, naming `performance.timeout`. It was accepted and
  never read.
- **A relative path setting exported in the shell** (for example `SLT_LIVE_CASES=samples`) is
  resolved once, against the directory `striim-test` runs in, and the run uses that folder.
- **`sync-framework.py` refuses (exit 2) to run inside the framework checkout** unless `--home` or
  `FRAMEWORK_CHECKOUT` names one. Copy the template or example out first; consumer repos pick this
  up by copying the new script from the template.
- **`expect_halt_contains` on a halt while running is matched only in server-log records that
  name the case's namespace.** A record is a timestamped line plus its continuation lines. Another
  app's identical error text no longer satisfies it.
- **`expect_halt: true` without `expect_halt_contains` is deprecated.** It loads with a warning,
  `striim-test doctor` shows it, and a later release refuses it.
- **A manifest the loader refuses is a collection error (exit 2)**, whatever `-m` selects, instead
  of a run-time failure, or of silent deselection when a depth was misspelt. A manifest without
  `depth:` is marked `depth_regression`.
- **The `.slt.json` sidecar and `evidence.json` record an expected failure as `xfailed` and an
  unexpected pass as `xpassed`**, where they said `skipped` and `passed`. An `xpassed` case never
  qualifies, and the run ends with an `XPASS` section naming each one. JUnit and `results.json`
  are unchanged.
- **An executed XPASS or XFAIL counts as executed for the infra guard**, so a run in which an
  xfail'd case ran beside a case skipped for missing infrastructure keeps its exit status instead
  of reporting that nothing executed. A build-cause skip still fails the run. Halt-log records are
  split per app node, so a record never runs from one node's log into another's.
- **`INT_STACK_PREFIX` is checked as `SLT_STACK_PREFIX` is**: lowercase letters, digits and
  dashes, starting with a letter or digit (`../x` or `Alt` was used as given). An invalid value
  stops an integration run before collection, as a usage error naming the setting, and
  `inttest.cli` exits 2. `SLT_STACK_PREFIX` with a trailing newline is refused.
- **A case without a `lifecycle:` block bounds its best-effort teardown**: 60 s for each step and
  600 s for the whole teardown, the ownership ledger's bounds. A hung delete is reported as a
  failure, which fails the case, instead of hanging the run. A delete that outlives its 60 s is
  still waited for, within the 600 s, before the case ends and before another delete on the same
  database or client starts; one still running at 600 s stops the run after that case.
- **`checkpoint_history: {predates: …}` fails when a read after the seed shows no checkpoint.**
  The history survives a kill restart, so an empty `interrupt` or `restored` read after a
  non-empty `seed` read proved nothing and passed. It now fails, naming the read.
- **The Striim primary's Java debugger (JDWP) is off by default.** It listened on every interface,
  port 8787, with no authentication, and the port was published on every host interface.
  `SLT_STRIIM_DEBUG=1` turns it on, published on `127.0.0.1` only (`SLT_STRIIM_DEBUG_HOST_PORT`,
  default 8787). The image rebuilds once, because its entrypoint changed, for a run that deploys
  the cluster; a running cluster that a shared run reuses keeps the debugger until it is stopped
  and started again (docs/UPGRADING.md).
- **A framework run publishes service ports for this machine and its containers only**:
  `127.0.0.1`, plus the Docker bridge gateway on a Linux engine (where `host.docker.internal`
  leads). They were published on every interface, with fixed, public credentials.
  `SLT_BIND_ADDRESS` publishes on one address you choose (`0.0.0.0` is every interface). A compose
  file run by hand with `SLT_BIND_ADDRESS` unset still publishes on every interface. A kept
  stack that shared runs reuse keeps its old, all-interface ports until it is stopped and started
  again; docs/UPGRADING.md has the steps.
- **Third-party Striim image downloads are checked against shipped SHA-256 digests**
  (`scripts/live/services/striim/deps.sha256`): the JDBC drivers, the JMX agent and Oracle Instant
  Client. A download or a file already present that does not match stops the run, naming it. Each
  Striim `.deb`, for which Striim publishes no checksum, is pinned the first time a machine sees it
  and checked before every later build. A file is hashed again only when its size, mtime or ctime
  changed since it last matched. Before, any non-empty file was trusted.
- **Integration: one test item per variant group.** A case with variants becomes several items,
  named `case[variant]` (`case[a+b]` when variants share a service). `-m <service>` now runs only
  that service's variants. Scripts or reports that name items by id see the new ids.
  `INT_VARIANT_ITEMS=0` restores one item per case. `INT_EMPTY_SELECTION_OK=1` lets a `-m` that
  selects nothing exit 0. (#90)
- **Integration: a run in which every case skipped exits 1**, when the cases skipped because the
  host could not run them. `INT_ALLOW_ALL_SKIPPED=1` opts out, except when a build was the
  cause. `striim-test doctor` now predicts what would skip or refuse a run, and `--strict` fails
  on those warnings. (#62)
- **Each live and integration run writes per-case records** to
  `<project>/.local-evidence/case-records`. `SLT_CASE_RECORDS` names another directory, and `off`
  writes none. Add `.local-evidence/` to your repo's `.gitignore`; the consumer template now has
  it. (#78, #79)
- **`assert.diff` times are measured differently.** The lag clock starts when the app starts, or
  when the last `post_start` input lands, instead of at the first poll. Each poll counts the
  target before reading it in full. A target already equal at the first poll is marked
  `elapsed_upper_bound: true`. `elapsed_s` values are smaller on large tables, so compare times
  only between runs on the same framework version. (#56, #60)
- **Lifecycle `initial-load`:** an app that finishes its load (COMPLETED or QUIESCED) before the
  RUNNING check now passes that check. Other modes fail at once on either status, instead of
  waiting for the deadline. (#54)
- **Integration under `SLT_INFRA_OWNERSHIP=shared`:** a running, healthy shared service is reused
  and never recreated from a second checkout. An unhealthy one, or one stopped from another
  checkout, fails the case and names that checkout. (#81)
- **GoldenGate trail writer:** an UPDATE that changes the key is written as UPDATEPK with the
  before-image key, so a key change reaches the target. (#72)
- **The Docker Striim image runs with `LANG`/`LC_ALL` `C.UTF-8`**, so the JVM writes non-Latin
  text correctly. (#76)
- **`INT_SHARED_SERVICES` is removed.** It was documented as reserved and only half worked: it
  skipped the integration session lock and the session-end teardown, but cases still started
  their services, and a serial session's `${TID}` stayed empty, so two sessions wrote the same
  tables. The setting is no longer read. Use `INT_STACK_PREFIX` for a second stack.
- **Switches are `1` or `0`, and nothing else.** Every on/off setting in both engines and the CLI
  (`SLT_KEEP_SERVICES`, `SLT_KEEP_RESOURCES`, `SLT_SKIP_VERIFY`, `SLT_PARALLEL`,
  `SLT_RUN_DISABLED`, `INT_ALLOW_ALL_SKIPPED`, a service's `opt_in_env` and the rest that
  docs/SETTINGS.md lists) is on at `1` and off at `0` or unset (`SLT_PRE_UP` and
  `INT_VARIANT_ITEMS` stay on when unset). Any other value, `true` and `yes` included, stops the run
  with exit 2 before any test, and `striim-test doctor` names it. Before, most switches were on
  for any non-empty value, so `SLT_SKIP_VERIFY=0` skipped every assertion and
  `INT_ALLOW_CONCURRENT_SESSIONS=0` dropped the session lock, and `SLT_RUN_DISABLED=0` ran
  disabled live cases while the integration tier skipped them.

- **`livetest.cli stop` refuses while another live run uses the stack.** A stop is `down -v`
  (databases, volumes, the Striim MDR). It now exits 2 while another live session is registered
  on the stack's Striim endpoint (a live shared marker, or a live exclusive run other than the
  caller's), naming their pids and the same command with `--force`, which stops anyway. With no
  other live session the stop runs as before. `livetest.preflight --restart-app-nodes` under
  `shared` refuses the same way. The check and the stop are one step: from the check until the
  stop or restart has finished (or failed), a live run declaring on that endpoint is refused,
  naming the stopping pid, so no session starts on a stack that is being removed. The endpoint is
  the one a run uses: `STRIIM_URL` from the environment, else from `.env`.
- **A failed integration teardown fails the session.** At session end a `compose down` that fails,
  or leaves a same-named container behind, sets exit status 1 (case results stand), and the
  service stays in `.int-services-up.json` so the next session tears it down. Before, it was a
  printed warning and the record was dropped.
- **Integration session and compose locks are machine-wide** (`<SLT_LOCK_DIR>/.int-session.lock`
  and `.int-compose.lock`, prefixed by `INT_STACK_PREFIX`). Two checkouts, or two `SLT_STATE_DIR`
  values, on one integration stack are now one stack: a second session is refused instead of
  taking down the first one's containers. Like the live tier's locks they are made writable by
  every user on the machine, whatever the umask, so another user's session on the same stack waits
  for them instead of failing on a permission error. `inttest.cli start` and `stop` read `.env` as a
  session does, so an `INT_STACK_PREFIX` set only there scopes their compose lock too.
- **A replaced `subprocess.run` no longer turns off the Docker commands' process-group kill.** A
  plugin or shim that wraps `subprocess.run` used to make every bounded Docker command call it
  directly, without its process group or its kill at the bound. A test that fakes the framework's
  Docker commands patches `livetest.dockercmd.run` (or passes `run=`), not `subprocess.run`.

**New**

- **A failed integration teardown is shown and counted.** When a case's teardown cannot reset its
  Postgres objects or drop its Oracle or Spanner tables, the warning now reaches the terminal as
  it happens, the run ends with an "integration teardown left objects behind" section, and the
  case record carries the failed steps in `teardownLeaks`. Before, it was a captured print that
  only `-rP` showed. The case keeps its outcome.
- **Sample `19-expect-halt`**: a case that passes because the app halts, with
  `expect_halt_contains` naming the case's own missing table. `AGENTS.md` now points at the
  framework gallery (`scripts/live/regression/framework/`) for the `test.yaml` features no sample
  uses.
- **Sample `20-spanner-json`**: SpannerBatchReader copies a table with a Spanner `JSON` column
  through SpannerWriter, and `assert.json` compares each document by type and array order.
- **One shipped integration case**, `scripts/integration/regression/op/referenceop/referenceop-adds-processed`:
  the case `docs/INTEGRATION-TESTS.md` walks through, in the tier's default case root, so
  `striim-test run --tier integration` in the clone runs it. `samples/` stays live-only.
- `assert.data`, `assert.diff` and `assert.json` accept `stable_seconds`, as `assert.file` does:
  after every spec first passes, keep reading for N seconds, and fail on a late change. On all four
  tiers it must be a finite number of seconds up to 2147483.647; `.inf` and `.nan` are refused at
  load.
- The build matrix file is named by `SLT_BUILD_MATRIX_ENV`. `FTC_PUBLISHER_RELEASES_ENV` still
  works when the new name is unset; it is deprecated and will be removed in a later release.
- `assert.monitor` (integration) takes `ADAPTER_DETAIL`, `LAST_BATCH_INFO` and
  `DATABASE_WRITE_INFO` by subset; `TIME_TAKEN_FOR_MERGE` is refused as a clock.
- Recovery evidence carries UTC times (`issuedUtc`, `deadlineUtc`, `startedUtc`, `observedUtc`)
  beside the monotonic ones; role node faults record `appAffected` and `appStatusOnNode`.
- `striim-test run` prints `<tier>: no tests selected` for a tier that selected nothing; beside a
  passing tier the run still exits 0. Every `run`/`list` option has `--help` text.
- acme-retail's `scripts/validate-example.py` checks any number of cases, deriving each case's
  app, services and table from the case, so a case added as in PROMPTS.md step 6 passes.
- `SLT_STRIIM_TRYOUT_JARS`: run the Docker cluster with replacement Platform or Agent jars, and
  record them in each case's evidence. See [docs/TRYOUT-JARS.md](docs/TRYOUT-JARS.md). (#53)
- `SLT_UI_FORM`: save each component as the Flow Designer would before deploy (`replay`, or
  `browser` with headless Chromium; optional extra `ui`). (#64, #65, #66, #69, #70)
- `recover.app`: stop or quiesce an application other than `${APP}`. (#67)
- `alter_recompile`: `interrupt: quiesce`, and `server_files` placed once the recompiled app is
  running. Trail writer: optional GoldenGate TxnID/CSN tokens. (#71, #72)
- A `postgres-admin` route for in-doubt commit cases (a short list of superuser statements). (#73, #74)
- A service may declare `family:`. BigQuery handling follows the family, so a repo can ship its
  own BigQuery emulator under another name. (#80)
- A Bigtable emulator service on both tiers, as groundwork: table and row helpers only, with no
  manifest setup or assertion yet. Its overlay is always applied to the Docker cluster. (#85)
- Bigtable setup and assertions on both tiers. `setup: bigtable:` creates each table (column
  families, GC rules, seed rows) before the case's `ddl:` and drops it at teardown unless the run
  is kept; a table that already exists is refused in the live tier. Live `assert: bigtable:`
  reads each table whole through its route and compares it to `expected`, and can be named in an
  `xfail`'s `tiers`. Integration `assert.target` on a `bigtable-*` route names the table in
  `query` and compares its rows. Both need `requires: [bigtable]`. The emulator is pinned by index
  digest for arm64 and amd64. (#96)
- Integration: a stock `mariadb` service and route (#92); per-event recovery `position` and
  resume by position (#84); `INT_VARIANT_WORKERS` to drive variant groups concurrently (#87);
  `assert.monitor` for `DDL_METRICS` (exact) and `ADAPTER_BASIC` (subset) (#93); mock source
  fields up to 1600 columns (#94).
- A prompt template for writing tests for an application you already have (#52); the README
  links a ParallelLoader quick start (#55).

- `SLT_SHARED_PROVISION=0`: under `shared`, a missing service is not started; the case fails and
  names `SLT_SHARED_PROVISION=1 python -m livetest.cli start <service>`, which starts it from the
  same shell or lane. Unset or `1` keeps today's behaviour. Meant for
  lanes that must never start a shared service. `livetest.cli stop --force` overrides the new stop
  refusal.

**Checks and fixes**

- On mssql and teradata, `min_rows` no longer passes on an uncommitted count when the writer
  still holds its lock at the timeout; it fails with "committed count never readable".
- The code samples (`samples/code/op`, `samples/code/udf`) count every event and row exactly
  (`exact:`, `events:` with `multiset:`) and prove completion with `lifecycle:`. They now run in
  Docker mode only. `samples/live/15-fault-evidence` is folded into `14-progress-recovery`.
- Docs: the integration comparison contract is JSON-level (boolean, integer, decimal, string;
  decimals by value, scale ignored). Java numeric width is not compared.
- The framework's three hermetic suites run on every pull request and push to `main`
  (`.github/workflows/hermetic-tests.yml`: Linux, Python 3.12, no Docker, no Striim), with each
  suite's skips listed and held to a budget (`tools/check_skips.py`). `tools/release.py` runs the
  same three commands as `CONTRIBUTING.md`; a test keeps the three lists the same.
- Perf items carry a marker for every service they require, so `--perf -m sqlserver` (and
  mysql, mariadb, teradata, vertica) selects them. `inttest.cli start`/`stop` use the session's
  compose lock under `INT_STACK_PREFIX`.
- `tools/prepare-recovery-goldens.py` checks that the source is the stated formula, not only that
  the copies equal it. The hermetic integration suite no longer takes, waits for or blocks the
  real session lock in the state directory the shell names.
- Docs: settings come from four layers (the framework clone's `.env` is read under a project
  root, below the project's); `${TID}` is never empty in a live case; Docker mode's memory
  settings, sample 17's `SLT_ENABLE_NODE_FAULTS=1`, `op.on_agent`, diff `fold_names`, the
  `stop_start_cycle` defaults and the integration `expect_log`, `monitor` and `exception_store`
  tiers are described; the by-hand build recipe computes the series of a four-part release; the
  Teradata and ServiceNow settings say which are required and that neither has a container;
  RUN-YOUR-FIRST-TEST.md lists every command and option.
- Pre-flight failure reasons in the outcomes file and log no longer carry credentials in a quoted
  key (`{"password": ...}`), a URL, an `IDENTIFIED BY` clause or any known secret value. URL
  credentials are removed up to the last `@`, so a password containing `@` leaves no tail.
- A `slt-image-probe-<pid>` container left by an interrupted run is removed by the next image check
  once its process has gone, so it no longer pins the Striim image.
- The framework's hermetic live suite no longer deletes the provision registry in the state
  directory the shell names.
- Framework tests: `recover:` and the `stop_start_cycle` / `service_outage` actions run
  hermetically through the plugin; timing-sensitive tests hold on a loaded host; tests that read
  git history skip, with the reason, outside a git checkout (`git_checkout` marker); the gallery
  gate's tier list must equal the loader's; and `pgclient`, `dbroutes`, `gcsadmin` and
  `uibrowser` have hermetic tests. Tests only; no behaviour change.
- A file read that catches the last event mid-write is polled again. A torn event with complete
  nested objects (`data`, `metadata`) was raised as a corrupt JSON stream on the first poll,
  ending the poll with no record; the exact-file route had the same check. An event missing its
  closing brace with more events after it is still a corrupt stream, raised at once. Inside a
  `stable_seconds` window a mid-write read is read again until the window closes, even past
  `timeout`; a file that still ends in a partial event then fails as unreadable, not as a changed
  result.
- The `vertica-admin` route keeps the server's search path. (#57)
- Spanner `ARRAY<JSON>` and `jsonb[]` elements are read as JSON, not `{}`. (#63)
- gRPC's fork log line no longer fails a Spanner or BigQuery case's inventory listing. (#61)
- Integration mock and stand-ins: the monitor types `DDL_METRICS`, `LAST_BATCH_INFO`,
  `DATABASE_WRITE_INFO`, `TIME_TAKEN_FOR_MERGE`, `ADAPTER_BASIC` and `ADAPTER_DETAIL`; the
  exception-store publish route and stemma paths; the `Position` constructors; and cached and
  compact paths, as the platform has them. (#75, #77, #83, #86, #88, #89, #93)
- Framework tests: the seven `assert:` tiers no test reached are now exercised through the plugin,
  as are the Postgres reset guard and the three surplus-data refusals (a target with extra rows,
  an extra emitted event, an unexpected emitted key). Tests only; no behaviour change.
- Internal: the paths core block carries `SLT_STRIIM_TRYOUT_JARS` in both engines, and the
  settings reference and a fault-evidence test are fixed. (#58, #59, #68, #91)

- Shared markers: the pre-flight entry points (`livetest.preflight`, `livetest.cli start`) remove
  their marker before returning; a marker records its process start time, and a marker of a dead
  or reused pid is removed whenever the endpoint registry is read. An earlier release's marker
  (no start time) counts only while its pid's process started before the marker was written.
- Shared service adapter: a provision record whose container is no longer running no longer turns
  the bring-up into a no-op reported as `provisioned-and-kept`; the record is dropped and the
  container started. One provisioning lock per container, so a fast service no longer waits
  behind a slow one's cold start. That holds in a parallel run too: the provision registry's lock
  is now taken only to read and update a record, and the lock held across a bring-up is that
  container's own.

## v0.3.2 (2026-10-27)

**Breaking changes and changed behaviour**

- **Integration: a whole-number `BigDecimal` is written as a decimal.** Oracle returns
  `NUMBER(10,2)` 12500.00 as `BigDecimal` 12500, which was written as `12500` and failed the
  type-strict compare against `12500.00`. It is now `12500.0`. A golden that recorded a
  whole-number `NUMBER`/`NUMERIC` value the operator emits as an int (`5001`) now needs
  `5001.0`. (#51)

## v0.3.1 (2026-10-26)

**Checks and fixes**

- `striim-test run`: a timed-out or cancelled runner's process group is no longer reported
  `processGroupCleared: false` while macOS is still reaping it (a transient EPERM). (#50)

## v0.3.0 (2026-10-26)

**Breaking changes and changed behaviour**

- **Unknown `assert:` tiers are refused** at manifest load in both engines (the live run
  fails the case; `striim-test doctor` exits 2). They were ignored, so a typo such as
  `rowcount:` dropped the assertion and the run passed. For a row count use `data:` with
  `rows:`. (#45)
- **Exit codes:** a failure plus a skip exits 1, not 3 (precedence 2 > 4 > 1 > 3). A failed
  OP/UDF build fails the case instead of skipping; missing prerequisites (no `mvn`, no JDK,
  no Platform jar) still skip. A prebuilt jar without the expected suffix fails. (#45)
- **Integration mock platform matches Striim 5.4.0.6C:** `WAEvent` allocates only its
  presence bitmaps, `setData`/`setBefore` do not grow the arrays, and bitmaps hold 7 columns
  per byte. An OP that relied on the old leniency now fails here as it would in production. (#46)
- **Type-strict comparisons:** integration `assert.data`, perf correctness and `assert.json`
  no longer treat `true` as `1` or `5.0` as `5`; decimals compare exactly. (#46, #47)
- **Integration `expect_error`** matches only the exception chain the operator threw, not
  log text or a harness timeout. (#46)
- **Assertions that passed on missing data now fail:** a missing Kafka topic is not 0 rows;
  an exact `assert.file` with no output file fails; `assert.monitor` fails on an empty
  response; golden rows with more cells than the header are refused;
  `checkpoint_history: empty` must hold for the whole `timeout`. (#47)
- **SQL Server and Teradata `min_rows`** wait for committed rows; `rows:` always needs
  committed rows. Oracle NUMBER values a float would round compare exactly, and timestamps
  compare to the nanosecond. (#47)
- **OP-loader poison:** a shared or undeclared run fails the case with `[slt-op-poisoned]`
  and names `python -m livetest.preflight --restart-app-nodes`; app nodes are restarted only
  on an exclusive run. (#48)
- **MySQL on your own instance:** the framework creates only the configured schemas; it never
  creates or alters accounts or grants privileges. Create the accounts yourself. (#48)
- **Cancelling `striim-test run`:** Ctrl-C, SIGTERM, SIGHUP or SIGQUIT is passed to the tier,
  which gets up to 200 s to clean up the shared stack; a second Ctrl-C stops it within about
  10 s. (#48)
- **`op.upload`:** `to:` must be a plain file name. **`action.concurrent`:** `db:` must be a
  route of a `requires:` service. **`seed_when: post_start` with `matrix:`** is refused. (#45, #46, #48)

**Operator jars**

- With several releases of one Striim series in the `STRIIM_BUILD_*` matrix, each release's
  jar is built in its own directory and published as `<Op>-<suffix>.jar` (the shortest unique
  version, for example `-5.4.0.jar`). A shipped `-<series>.jar` is still accepted as a
  prebuilt. Add `.release-build-*.xml` to your repo's `.gitignore`. (#37, #41)

**Checks and fixes**

- `assert.gcs` records `sha256:<hex>`, so a passing run writes valid evidence. (#39)
- SQL Server: a deadlock-victim read (1205) during a diff poll is retried. (#40)
- `checkpoint_history: {predates: <seed>}`; Teradata reads use `NOWAIT` and teardown drops
  views. (#38)
- `SLT_INFRA_OWNERSHIP=exclusive` with `--parallel` tears its stack down; `KAFKA_ZOOKEEPER`
  follows the services host. (#48)
- Recovery samples ship their goldens; selection advice says tags select only in a direct
  pytest run. (#44)
- macOS arm64 test lock: pymssql 2.4.2 (macOS 14 or later). (#43)
- The docs describe unknown `assert:` tiers as refused. (#49)

## v0.2.0 (2026-10-19)

**Breaking changes and changed behaviour**

- **Timed recovery requires RUNNING:** a completed or halted app at `recover.after`
  fails `fault-missed`. Keep work in flight at the boundary, or use a bounded
  progress trigger. Progress recovery also refuses a target that reaches its upper
  bound while the interruption takes effect. (#22)
- **Tags affect selection:** in a direct pytest run of the engine, `-k` also matches
  manifest tags and can select more cases. Each tag has a `tag_<name>` pytest marker,
  for example `-m tag_p1`. `striim-test run` takes neither flag; its PATH, `--suite`
  and `--case` selection is unchanged. (#27, #34)
- A DEPLOY/START refusal with `expect_halt: true` no longer passes while silently
  skipping declared post-start work, lifecycle or assertions. Use a matching
  `expect_halt_contains` for an intentional startup refusal; evidence records what
  was skipped. (#9)
- **Oracle resource retention:** case-prefixed observer views and owned logon
  triggers are removed even when apps/tables are retained. Existing cases relying
  on retained prefixed views must use the documented observer cleanup contract.
  Unprefixed ordinary views retain their previous behaviour. (#10)
- GCS/Spanner credential uploads now fail setup immediately if any eligible app
  node cannot receive the key, rather than continuing with missing credentials. (#11)
- Smoke settling is now one second instead of five; a smoke pass does not prove
  a slow asynchronous initialization completed. Post-start seeds without an
  explicit `after` wait at least five seconds after RUNNING when neither the reader
  nor lifecycle has proved readiness. (#3, #4, #5)
- Integration target events now enter through `receive` under the writer's
  `synchObject`, matching platform event delivery; `flush` remains outside that
  lock. Rebuild the integration harness when updating. (#32)

**Opt-in features and settings**

- **Node faults:** `recover.node` skips before provisioning unless
  `SLT_ENABLE_NODE_FAULTS=1` selects a dedicated disposable stack. The opt-in
  publishes the secondary API (`SLT_STRIIM_NODE_HTTP_HOST_PORT`, default 9082).
  Native mode skips named-node faults. See [UPGRADING.md](docs/UPGRADING.md) before
  reusing an older stack. (#22, #36)
- **Oracle observers:** `oracle-admin` supports scoped views and logon triggers
  in the data PDB, with `SLT_ORA_ADMIN_USER` and `SLT_ORA_ADMIN_PASSWORD`.
  Observer probes identify themselves at login; teardown removes observer hooks
  even when ordinary resources are kept. Existing Oracle databases need the
  documented direct grants; rebuilding an image alone does not update them. (#10, #36)
- **BigQuery emulator:** supply `SLT_BIGQUERY_IMAGE` for bounded scalar exact
  reads, legacy streaming, direct CSV batch loads and non-optimized batch MERGE
  sentinels with a Postgres source. Historical Linux amd64 qualification used
  earlier emulator images; the currently required managed image still needs
  matching fresh qualification. The Java exact-chunk boundary is excluded.
  UTF-8 CSV needs the documented JVM setting.
  Native ARM, Storage Write, recovery and cloud equivalence are unqualified. (#11)
- **Real Cloud Spanner:** the integration tier accepts `INT_SPANNER_CREDENTIALS`
  and pre-existing project/instance/databases. Unset emulator endpoints and use
  `SLT_PARALLEL=1`; SQL and teardown require case-prefixed objects. The framework
  creates no instance or database. (#33)
- **Postgres idle transactions:** `SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS` and
  `INT_PG_IDLE_TRANSACTION_TIMEOUT_MS` optionally set positive millisecond budgets
  on disposable data roles, including new Striim connections. Unset retains no
  framework role policy; zero is refused. Allow for deliberate held transactions
  and recovery. External databases keep their policy. (#24)
- **Fixture deadlines:** `fixture_timeout` overrides `SLT_PG_FIXTURE_TIMEOUT` for
  Postgres DDL and seeds. Both unset preserve server statement/lock policy.
  Enabled budgets also bound lock waits; passive blocker diagnostics remain on
  without a budget. Concurrent action DML keeps its existing SQL path. (#23)
- **Teradata JDBC:** `SLT_TERADATA_JDBC=1` fetches the optional driver and builds
  it into the Striim image. Unsetting stops the fetch; without a dependency
  manifest, remove an already staged driver to omit it from a rebuild. Teradata
  cases skip or fail with instructions when it is missing. Driver licence terms
  apply.
  The settings example and service guide cover this choice. (#28, #29, #30)
- **Prebuilt jars:** select the exact OP/UDF artifact with
  `SLT_PREBUILT_JAR_<MODULE>`, `SLT_PREBUILT_JAR` or `SLT_PREBUILT_DIR`, in that
  order. Both engines validate the module, release series and Java level and
  report the selected bytes' SHA-256 instead of building the module. (#2)

**Checks and fixes**

- Typed exact comparisons cover both Spanner dialects and Kafka records.
  Strict JSON distinguishes invalid or duplicated keys; raw-file checks cover
  complete inventories and bytes. Bounded absence requires a qualifying completion
  sentinel. Postgres-source CDC sentinels also support Spanner, Kafka and Docker
  files; unsupported routes remain documented explicitly. (#25)
- Controlled Postgres transaction seeds can overlap, commit or roll back across
  recovery. Consistent multi-table snapshot histories check committed prefixes;
  append-only audit checks count duplicate deliveries rather than hiding them
  behind final state. New samples demonstrate each contract. (#26)
- Recovery can interrupt inside a bounded progress window, record observed fault
  boundaries, and require a fresh post-restore marker. Named-node Docker faults
  verify live placement and survivor APIs. When reusing a contributor environment,
  run `python3 tools/prepare-recovery-goldens.py` for the shared sample CSV copies;
  the test-environment helper also prepares them. (#22)
- SQL Server seeds can use `mssql-admin` for privileged statements. Nothing is
  dropped through this route. (#1)
- Service-token discovery is reused across manifest loads until its inputs change,
  reducing collection work without retaining stale service declarations. (#8)
- Integration mocks provide `RetriableWriter.getOwnerFlow`, its `Flow` return type,
  and the `BatchAcknowledgeable` marker, so compatible adapters load without missing
  platform types. The harness leaves the owner flow unset off-server. (#31, #35)

Tested on Striim 5.4.2: Postgres samples 01–04 and the two code samples in
Docker mode on Linux x86-64; samples 01, 02, 04 and the code samples in native
mode on Linux; sample 01 in Docker mode on Apple Silicon. Striim 5.4.4.0 was
qualified on Apple Silicon at framework `44ab472`
with 31 field live-gate passes and 202 regression passes (2 skips), excluding
Spanner, Teradata and MSJet. These are recorded qualifications, not a claim that
all new features or this release commit passed live. (#7)
See [WHAT-IS-SUPPORTED.md](docs/WHAT-IS-SUPPORTED.md) for route-specific qualifications.

## v0.1.0 (2026-10-08)

First public release.

`striim-test` runs tests of Striim applications against a real Striim. A test is a folder: the
app's TQL, its DDL and seed data, the expected rows, and a `test.yaml`. The framework creates the
tables, deploys and starts the app, waits for it to finish, compares the target with the expected
rows, and cleans up.

- **Where Striim comes from:** your own server (`STRIIM_URL`), or a two-node cluster with an agent
  that the framework builds and starts in Docker. Docker mode needs your own Striim license.
- **Services:** PostgreSQL, MySQL, SQL Server, Oracle, Kafka, Vertica, the Cloud Spanner emulator
  and a GCS emulator, started in Docker or pointed at your own instances; ServiceNow through your
  own sub-production instance. See `docs/SERVICES.md`.
- **Checks:** row counts, golden files, source-to-target diffs, typed exact comparison and
  lifecycle witnesses (PostgreSQL routes), file output and JMX (Docker mode).
- **Your own Java:** Open Processors and UDFs are built and loaded into the app under test
  (`docs/TESTING-YOUR-JAVA.md`).
- **Your own repository:** keep tests beside your apps, pin the framework with `framework.pin`, and
  start from `templates/consumer-repo/` or the worked example in `examples/acme-retail/`.
- **Results:** exit codes 0 to 5, JUnit XML for CI, and an evidence record per case.

Tested on Striim 5.4.2: Docker mode on Linux x86-64 and macOS on Apple Silicon, native mode on Linux.
Support details are in `docs/WHAT-IS-SUPPORTED.md`. Start with `README.md` or `docs/reference.html`.
