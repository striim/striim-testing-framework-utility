# Lifecycle, run identity and ownership (design notes)

Modules: `livetest/runident.py` (identity), `livetest/infra.py` (infrastructure ownership),
`livetest/lifecycle.py` (the `lifecycle:` block, witnesses, probes), `livetest/ownership.py` (the
ownership ledger and cleanup). The plugin hooks that call them (collection-finish declaration,
manifest `lifecycle:` key, report-hook evidence) are separate from these modules. Examples of the
block: `scripts/live/tests/fixtures/lifecycle-contract/lifecycle.*.yaml`.

## Infrastructure ownership (`infra.py`)

A live execution declares how it owns the Striim cluster and the service containers:
`SLT_INFRA_OWNERSHIP` ∈ {`exclusive`, `shared`}. `DEFAULT_INFRA_OWNERSHIP = None`: undeclared is
refused with an error naming the variable and both values.

Declaration points:
- `pytest_collection_finish`, when at least one live item was collected and the session is not
  collect-only; in the controller and every xdist worker. A refusal is a `pytest.UsageError`. An
  xdist worker never refuses at collection; the controller refuses from the selection the workers
  report, and a live item that still executes in a worker refuses when it runs.
- The pre-flight entry points (`livetest.preflight`) `provision`, `provision_cluster`, `restart_app_nodes` and
  the services-only `provision_services` declare before any registry clear or bring-up.
- `pytest_configure`, `striim-test list`, and `--collect-only` never declare.
- `_resolve_striim` never refuses by itself. It reads the declaration on the config: `shared`
  forbids redeploy, `exclusive` refuses a foreign endpoint, absent keeps the old behaviour (only
  direct in-process callers such as hermetic tests). Every non-test caller must sit behind an
  unconditional declaration; the AST scanner in `tests/lifecycle/test_infra.py` enforces it.

**Exclusive** — freshly allocated, leased, bound; never adopts or redeploys a cluster:
- `SLT_STACK_PREFIX` must be unset. The prefix is `x<sha256(SLT_RUN_EPOCH)[:8]>`, exported
  before any compose or docker call.
- Lease `<SLT_LOCK_DIR>/.slt-endpoint-<port>.exclusive` (non-blocking, held to
  `pytest_unconfigure`), with `<lease>.owner` (`runId`, `pid`). A same-run process joins; any
  other is refused; it is refused while a live shared marker exists. Holder and joiners register
  `<lease>.members/<pid>`; the run's claim lasts until its last live member releases.
- Any `<prefix>-slt-*` container refuses the run, naming the `livetest.cli stop all` command.
- A reachable endpoint or open port is refused before the provisioning lock. The verdict is
  decided once per run (`<lease>.endpoint`) and reused by later members.
- After provisioning, `<prefix>-slt-striim` must publish the URL's port. A member that did not
  provision binds only when its run proved the endpoint free.
- An identity mismatch is refused, never `cluster_down`. Destructive bring-up and teardown are
  allowed only here. One exclusive stack per host port.

**Shared** — registered, non-destructive, coordinated:
- `SLT_KEEP_SERVICES=1` is required. The run registers
  `<SLT_LOCK_DIR>/.slt-endpoint-<port>.shared/<pid>`; refused while the exclusive lease is held.
- Every service resolves through one adapter under `<SLT_LOCK_DIR>/.slt-shared-provision.lock`
  (bounded wait, 600 s, then `lock-timeout: …`). Inside the lock a running container is recorded
  `reused`; otherwise it is provisioned and recorded `provisioned-and-kept`. The parallel
  provisioning registry uses a different lock file.
- The cluster is reused when reachable, provisioned and kept when absent, never redeployed: an
  identity mismatch fails `shared cluster identity mismatch: …` and live items skip.
- All invocations on a host must use the same `SLT_LOCK_DIR`; the value is recorded.

Evidence `resources.infrastructure`: `{ownership, stackPrefix, lockDir, lease: {kind:
held|joined|shared-marker, path}, services: [{name, container, status}], striim: {status,
container, urlPort, boundToAllocated, provisionedBy}, teardown}` (a shared run against a native
Striim records `striim: {status: external, container: null, …}`); `teardown` is `null` or
`{status: failed, failures: [{resource, error}]}`.

## Identity (`runident.py`)

- `per_test = "t" + sha256(run id, xdist worker or "w0", case name)[:9]`.
- Tokens: `${TID}` = `per_test_` (never empty, serial runs included); `${TID_ORACLE}` /
  `${TID_UPPER}` (11 chars); `${NS}` = `SLT_<slug[:25]>_<per_test>` (≤ 40 chars, so `<NS>.<component>` stays ≤ 62 for a
  component name of up to 21 chars: SpannerWriter fails START with "Insufficient Privilege to get
  Dialect" at 64); `${APP}` =
  `${NS}.<slug>App`; `${PG_SLOT}` = `slt_<per_test>`; `${OWNED_DIR}` =
  `/opt/striim/slt-runs/<ns>`; `${RUN_ID}`, `${WORKER}`, `${ATTEMPT}`.
- The identity is probabilistic: recorded in full, never a delete authority by itself.
- The run id is `SLT_RUN_EPOCH`; the `striim-test run` CLI sets it for tier children to the run
  directory basename (`setdefault`).

## The `lifecycle:` block (`lifecycle.py`)

Optional. Without it a case runs as before and its evidence records `lifecycle.mode: legacy`
(readiness = app RUNNING, completion = assertion polling); such a run never qualifies.

```yaml
lifecycle:
  version: 1                          # only 1
  mode: initial-load | cdc
  sink: db | file                     # where completion is observed
  readiness:
    kind: baseline-landed             # initial-load: RUNNING + sink equals the positive source baseline
    source: {db: postgres-source, table: "${PG_SOURCE_SCHEMA}.${TID}src"}
    target: {db: postgres-target, table: "${PG_TARGET_SCHEMA}.${TID}tgt"}
    # kind: source-progress           # cdc; pg_replication_slots.active for ${PG_SLOT}
    # kind: sentinel                  # cdc, sink db; needs the sentinel block
  completion:
    kind: source-count                # sink db; equal and > 0
    # kind: row-count                 # sink db; db, table, expect > 0
    # kind: file-lines                # sink file; path ${OWNED_DIR}/...; lines > 0
    # kind: sentinel                  # sink db; the only kind for an empty final result
  stability: 5s                       # ms|s|m or seconds; finite, >= 0
  deadlines: {readiness: 60s, completion: <case timeout>}
  reset: owned                        # the only accepted value
  sentinel:                           # required iff a sentinel kind is used
    db: postgres-source
    insert: sentinel_insert.sql       # ${SENTINEL_ID}, ${SENTINEL_READY_ID}, ${SENTINEL_DONE_ID}, ${SENTINEL_TAG}
    delete: sentinel_delete.sql
    observe: {db: postgres-target, table: "${PG_TARGET_SCHEMA}.${TID}tgt", key: id}
```

Validated at load, before any provisioning (`LifecycleSpecError`, surfaced as a manifest error):
- `initial-load` → readiness `baseline-landed`; completion `source-count`, `row-count`,
  `file-lines`. `cdc` → readiness `source-progress` or `sentinel`; completion `sentinel`,
  `row-count`, `file-lines`. `sink: file` → no sentinel kinds.
- Zero is never a witness: `expect: 0` / `lines: 0` are rejected at load; a zero baseline or
  source count fails at run time with `zero-count`.
- Routes: only `postgres-source`, `postgres-target` and `sink: file` on the docker cluster.
- Every `assert.file` path and `server_files` dest starts with `${OWNED_DIR}/`, with no `.`,
  `..` or empty segments; the rendered path is re-checked before any read. Native mode does not
  support lifecycle file cases.
- `.inf` / `.nan` durations are rejected.

Run order: provision under the declared ownership → DDL → pre-deploy seed and the source
baseline → deploy → bounded RUNNING → readiness → post-start seeds → completion → stability →
assertions → cleanup → evidence.

Witnesses read only this attempt's resources: every table a witness reads must be a `confirmed`
ledger table of this attempt on the route it reads (`witness-not-owned`), a target equal to its
source is refused (`witness-self-observation`), and a file path must lie in a confirmed owned
directory.

Deadlines are monotonic. Failure reasons: `deadline`, `terminal-status:<st>`, `zero-count`,
`stability-lost`, `probe-cancelled`, `cancel-failed`. Postgres probes use their own connections
with `connect_timeout` and `statement_timeout`; nothing is sent after the deadline; a statement
in flight is cancelled, its connection closed and its thread joined within fixed bounds; a cancel
that hangs, or a statement finishing after the deadline, is `cancel-failed`. Server file reads
are commands killed at the remaining deadline.

Sentinel: each phase (ready, done) inserts a fresh id `1 + randbelow(2**31 - 1)`, observes it
present, deletes it, observes it absent. `${SENTINEL_TAG}` = `slt-<per_test>-<attempt>-<phase>`.
No other row ever matches; insert-only targets cannot use it. `seed[].after` stays a delay.

Records: `ready` / `completion` = `{kind, condition, witness, at, startedAt, endedAt, deadlineS,
observations (last 50), reason}`; `stability` = `{seconds, heldAt, measuredS, value, held}` from
a real re-read after the interval.

## Ownership ledger and cleanup (`ownership.py`)

Every case keeps a ledger at `<state dir>/lifecycle/ledgers/<per_test>.json` (state dir from
`layout.state_dir()`: a `set_roots(state=…)` or manifest `stateDir`, else `SLT_STATE_DIR`, else the
live engine dir), persisted before any side effect.

- **Acquire before create.** Each Postgres table and replication slot named by the rendered DDL
  gets a catalog pre-existence check before its file runs. An object that exists and is not in
  this identity's ledger is `collision:<kind>:<name>`: refused, never touched. After the file,
  only objects that now exist are confirmed. `CREATE PUBLICATION` / `CREATE SCHEMA` are refused
  before execution. The namespace must be absent (`LIST NAMESPACES`).
- **Deletes** only `confirmed` entries, by exact name; the owned directory as one unit (created
  with an exclusive `mkdir` per node, each node recorded as it succeeds).
- **Ledger identity.** Ledger version 2. A prior file is imported only when version and full
  identity (run id, worker, case, per-test id, namespace, app, slot, owned dir) match;
  otherwise `ledger-version-mismatch` / `ledger-identity-mismatch` refuses the case and nothing
  it lists is deleted.
- **Target binding.** Each attempt records `bindings.<attempt>` first: per Postgres route the
  endpoint and `pg_control_system().system_identifier`, the Striim URL, the docker app nodes and
  container ids. A prior entry is reset, and `replay` deletes, only on an equal target; a changed
  or unreadable target is reported foreign with a verification gap.
- **Intents.** An `intended` entry is never deletion authority (`unresolved-intent`, a gap).
- **Unsupported kinds.** Other engines' tables, Kafka topics, GCS buckets and OP uploads are
  never acquired or deleted (no prefix drop, no computed name); each is a verification gap.
- **Inventories.** A docker check is `present`, `absent` or unreadable; unreadable refuses
  acquisition and is a failed verification read, never absence.
- **Checkpoints** are deleted only by exact name when `<ns>.` is a whole component; every other
  listed name is reported. Foreign objects are reported, never deleted.
- **Bounded cleanup.** Each delete and verification read has a per-operation bound inside a
  per-case deadline. Cleanup never raises: an unexpected error is `cleanup.status: failed` with
  detail `cleanup-error:…`, and a test's own failure is never replaced.
- `cleanup` = `{status: ok|failed|skipped, detail}` (`skipped` carries its reason in `detail`); `skipped` under `SLT_KEEP_RESOURCES` /
  `SLT_KEEP_RESOURCES_ON_ERROR`.
- `SLT_LIFECYCLE_FAULT=cleanup:<table|slot|namespace|dir|file>` fails the first owned delete of
  that kind on purpose (the cleanup-failure control); such a run never qualifies.
- Kept or fault-injected runs: `python -m livetest.ownership replay <ledger.json>`.

## Qualification

`run.qualifies` is true only when a lifecycle block is present, every witness is `satisfied`,
cleanup is `ok` and verified with no verification gaps, and no fault was injected; the exact-data
conditions (design note on exact data) are added on top.

## Envelope and finalization

The plugin and evidence hooks that write these records are re-applied later; the rules below are what they must keep.

- **Path.** `<junit dir>/evidence/<item name>/<run id>/evidence.json`, named in the junit
  `<properties>` (`slt_evidence_json`, `slt_qualifies`). `run.caseId` carries the CLI case id;
  `runId` is `SLT_RUN_EPOCH` in full.
- **Written once, for every outcome.** The envelope is written from `pytest_runtest_makereport`
  for pass, fail, error and skip. It is validated before it is written (unknown keys, missing
  hashes, reasons outside the allowlist and a `run.qualifies` the document does not support are
  refused) and written exclusively: an existing envelope is never overwritten. New envelopes are
  never partial; an old partial envelope stays readable and never qualifies.
- **Cleanup never hides the case's own failure.** A cleanup failure raises only when no other
  exception is in flight. An unexpected error inside cleanup is `cleanup.status: failed` with
  detail `cleanup-error:…`; the case's own exception and the in-use lock release are unaffected.
- **Session teardown is part of the outcome.** `pytest_sessionfinish` runs `tryfirst`. When
  teardown of owned infrastructure (services, cluster) fails, the exit status becomes 1, each
  passed live testcase gets a JUnit `<error>` and `slt_qualifies=false`, the v1 sidecar marks it
  `error`, and each envelope gets `run.status: error`, `run.qualifies: false` and
  `resources.infrastructure.teardown`.
- **Evidence failures fold in the same way.** An envelope build, validation or write failure is
  an evidence error (`item._slt_evidence_error`, JUnit property `slt_evidence_error`): exit 1, each
  affected passed case gets a JUnit `<error>` and v1 `error`. `pytest_sessionstart` adds the
  JUnit global property `slt_invocation` when the junitxml plugin is present.
- **`resources`.** `{infrastructure, owned, reused, foreign: [{kind, name, note}],
  cleanupVerified, verificationGaps}`, with `run.qualifiesReason` beside `run.qualifies`.
- **Stability re-read.** For a sentinel phase the stability re-read is the sentinel's own id,
  which must still be absent; no stability record is written without that read.
- **Redaction.** Values of secret-named environment variables (`*PASS*`, `*SECRET*`, `*TOKEN*`,
  `*KEY*`, …) and the credentials a service resolution returned (substrings from 8 characters on,
  whole values and whole tokens below that), URL userinfo, the home directory and the host name
  never reach the envelope, the v1 sidecar details or the junit failure text. Hashes are computed
  before redaction.
