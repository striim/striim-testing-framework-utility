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
| `deployment_groups` | list | no | — | owned initial worker groups and members added after deployment; Docker only |
| `requires` | list[string] | no | `[]` | service names to provision/reuse (e.g. `postgres`, `oracle`, `mssql`, `kafka`, `spanner`, `gcs`; `teradata` and `servicenow` connect to your own instance, set with `SLT_TERADATA_HOST` / `SLT_SERVICENOW_HOST`) — each resolved once per session |
| `tokens` | mapping | no | `{}` | manifest-declared `${NAME}` values, rendered wherever the harness renders tokens (TQL, DDL/seed, `server_files`, `assert` targets, `kafka_cleanup_topics`, `op.upload` names). Names match `[A-Z][A-Z0-9_]*`; values are strings (numbers are cast) and may not themselves contain `${...}`. A name the harness sets -- `NS`/`APP`/`APP_BARE`/`TID*`, the placement and `STRIIM_*` tokens, a module's `${<TOKEN>_JAR}`/`_NAME`, any service's `provides:` (e.g. `MSSQL_URL`), and the reserved `SENTINEL_*` / `SENTINEL_RECOVERY_*` prefixes -- is refused at load, so merge order never decides. Lets one shipped app serve several manifests that differ in a value (e.g. `TrailFilePattern: '${SRC_TRAIL}'`, `k10*` vs `t10*`) |
| `kafka_cleanup_topics` | list[string] | no | `[]` | extra Kafka topics the test's **app** creates that the framework didn't derive itself — e.g. a persisted stream's derived `<ns>_<streamName>` data topic and its `<topic>_CHECKPOINT` companion. Each entry is `${...}`-token-rendered (embed `${NS}`/`${TID}` for per-test uniqueness) and appended to the same best-effort per-test topic delete that already removes the derived `${KAFKA_SRC_TOPIC}`/`${KAFKA_TGT_TOPIC}` at teardown. Requires `kafka` in `requires` (loader fails loud otherwise). Absent/empty (every test that doesn't set it) is a strict no-op |
| `ddl` | string \| list | no | none | schema setup, run once before seed/deploy. Plain string (or a bare list item) → the default `postgres-source` route. List form — entries in the same style `upload:`/`server_files:` use: `- file: <name>` plus an optional `db: <route>` (default `postgres-source`), applied in order. Routes are `<svc>-source`/`<svc>-target` (fixed `qasource`/`qatarget` schemas) plus the emulator routes (`spanner-*`, `gcs`, `kafka`) — see `SERVICES.md`. **Deprecated** (still parses, warns naming the file): the route-keyed entry `{source_db\|target_db: <route>, file: <name>}` — the route value already carries the direction; write `db:` |
| `seed` | string \| list | no | none | data setup, same shape/back-compat as `ddl`, plus two per-entry keys `ddl` does not take: **`when`** (`pre_deploy` (default) \| `post_start` \| `post_recover`) and **`after`** — see below |
| `action` | list | no | `[]` | app lifecycle operations run in order after every `post_start` seed and before every assertion tier. Five types: **`stop_start_cycle`** (`cycles`, `delay_before_stop`, `stop_duration`, optional `concurrent:` SQL loops on background threads, each `{db, file, loop_interval}`: `db` must be a route of a `requires:` service, and the case fails if a loop never succeeds or any of its statements errors); **`drop_recreate_app`** (`app` — token-rendered so a multi-app manifest names just the piece it rebuilds; `delay_before_stop`, `recreate_wait`, `seed`, and `capture`/`stopped_seed`/`tokens`, see [below](#action-drop_recreate_app--rebuild-the-app-from-its-tql)) which STOPs, `DROP APPLICATION … FORCE`s via `StriimClient._force_drop`, then rebuilds the app from its own `CREATE OR REPLACE APPLICATION … END` block in the rendered TQL and waits for RUNNING; **`service_outage`** (see [below](#action-service_outage--take-a-required-service-down-mid-run)) which stops a required service's container mid-run and starts it again; **`capture`** ([below](#action-capture--read-a-value-into-a-token)), which reads a `DESCRIBE` or `MON` field into a token; and **`alter_recompile`** ([below](#action-alter_recompile--replace-components-in-place)), the in-place `ALTER APPLICATION … RECOMPILE` upgrade. Gallery: `framework-action`, `framework-drop-recreate` |
| `assert` | mapping | **yes** | — | at least one tier (`smoke`, `data`, `diff`, `file`, `gcs`, `json`, `monitor`, `checkpoint_history`, `jmx`, `placement`) — see below |
| `example` | string | no | none | app directory, relative to the project root (`SLT_PROJECT_ROOT`, this clone when unset), with or without an OP/UDF. When set, `tql`/`ddl`/`seed`/`upload` are read from THERE; only `expected/*.csv` and entries marked [`local: true`](#local-true--test-only-files-beside-an-example) stay test-local. Absent → everything is relative to the test dir (self-contained regression style) |
| `op` | mapping \| list | no | none | build + upload OpenProcessor jar(s), registered via `LOAD OPEN PROCESSOR` — single mapping or a list, see below |
| `udf` | mapping \| list | no | none | build + upload UDF jar(s), registered via a global `LOAD` (`client.load_jar`) — single mapping or a list, see below |
| `server_files` | list | no | `[]` | place test-dir files onto the Striim server before deploy or after RUNNING, or upload and register a prebuilt jar (`load:`, from the test dir or outside it) — see below |
| `timeout` | int | no | `120` | seconds an `assert:` tier polls before failing |
| `fixture_timeout` | number | no | unset | Opt-in Postgres fixture statement deadline in seconds, independent of assertion `timeout`; positive finite value up to `2147483.647`, applies to DDL/slot setup and seed SQL including action seeds. Overrides `SLT_PG_FIXTURE_TIMEOUT`; if both are unset, server statement/lock policy is retained. An enabled budget also sets the lock deadline to min(15 seconds, half the statement budget). Concurrent action DML retains its previous unbounded behavior. |
| `tags` | list[string] | no | `[]` | free-form labels (e.g. `[op, oracle, p1]`). `striim-test run` does not select by tag. In a direct pytest run of the engine, `-k p1` uses case-insensitive substring matching against paths, tags and markers, while `-m tag_p1` selects the exact tag marker (case is preserved). Markers are registered automatically as `tag_<name>`, replacing each character outside ASCII letters, digits and `_` with `_` (e.g. `slow-path` becomes `tag_slow_path`); tags that sanitise to the same name share a marker |
| `disabled` | string \| bool | no | — | truthy => the test **skips** before any provisioning (collected + reported as skipped, never run). Use a non-empty string as the reason (e.g. `disabled: "top-level JSON_ARRAY writes {}; issue 123"`). For known-bug / quarantine repros that shouldn't fail CI. Force-run a disabled test with env `SLT_RUN_DISABLED=1` (e.g. to check whether the underlying bug is fixed). |
| `disabled_parallel` | string \| bool | no | — | same shape and skip point as `disabled`, but only takes effect when the run is concurrent (an xdist worker or `SLT_PARALLEL=1`) -- a serial run ignores it entirely. NOT for a test that races or collides with a sibling -- that's an isolation bug, fix the tokenization. NOT for a plain "table name too long" failure either -- shorten the name first (an Oracle table stays under Oracle's identifier-length budget once `${TID_ORACLE}` is prepended; see `_tid_oracle`'s docstring in `scripts/live/livetest/plugin.py`). Also required for a shared-node fault case that must be excluded from concurrent selection; this is a skip rule, not a lock. Otherwise only for a defect genuinely triggered by parallel-run token substitution that isn't fixable by renaming, with a linked ticket ref as the reason string. Also force-run-able with `SLT_RUN_DISABLED=1`. |
| `xfail` | string \| dict | no | — | the test is **expected to fail on its assertion** — a committed defect repro that asserts the CORRECT behaviour. String form is the reason; dict form `{reason, strict, tiers, releases}`. `strict: true` (default `false`) turns an unexpected pass into a failure. **`tiers`** names which `assert:` tiers the failure may come from (`data`, `diff`, `file`, `gcs`, `json`, `monitor`, `halt`, `checkpoint_history`, `jmx`; default all of them; **never `smoke`**): only an assertion failure on a named tier is reported XFAIL — a deploy error, a provisioning timeout, an app that never reached RUNNING, a failure on an unnamed tier, or a spec problem is reported as a **real failure**, exactly as it would be without the marker. Before this narrowing an xfail said green for a TQL typo. Run with `--runxfail` to see every outcome as it is. See the section below. |
| `expect_halt` | bool | no | `false` | `true` => the app is expected to reach a terminal status (HALT/CRASH/TERMINATED/DEPLOY_FAILED) instead of RUNNING -- a raise-path test. This can be satisfied at any phase: a synchronous failure during DEPLOY/START itself (the TQL-import call raises before the app is even running), the app never reaching RUNNING during startup, or a later terminal transition while the app is running (polled the same way as any other `assert:` tier). Alone, `expect_halt: true` satisfies the manifest's "at least one assertion" requirement -- no `assert:` block is needed. To also pin *why* it halted, add `expect_halt_contains`. A DEPLOY/START failure skips everything the case declares to run after start: `post_start` seeds, generate specs and server files, `lifecycle:`, and every `assert:` tier. If the case declares any of these, a DEPLOY/START failure fails the case and names them, unless `expect_halt_contains` matches the failure text. Set `expect_halt_contains` when a startup refusal is the expected outcome. The passing record then lists what was skipped. |
| `expect_halt_contains` | string \| list[string] | no | — | narrows `expect_halt` from "any terminal status for any reason" to "a halt whose reason names these substrings". A list means **ALL** substrings must be present (AND), not any-of — pick the one substring that's stable across Striim versions if the phrasing varies. Requires `expect_halt: true` (set alone it would be a silent no-op, and is rejected at load). Where the reason text comes from depends on the phase: a synchronous DEPLOY/START failure matches against the raised import error, which is always available; a **polled** terminal transition matches against the app-node server-log tail, which only exists in **docker** mode — on native single-node (or if the tail read itself fails) the test still PASSES but its record detail says `halt reason not checked (native mode)`, so grep `.slt.json` for that string if you need to know whether the reason was actually verified. Caveat for the polled/docker path: only the server-log text written since this case deployed is searched (a node whose log size could not be read at deploy is not searched). It is not scoped to this test's app — under `SLT_PARALLEL` a sibling test's identical error text written meanwhile could produce a false pass. Pick a substring unique to your scenario (e.g. the tokenized table/app name), not a generic phrase. |
| `recover` | mapping | no | — | interrupt the app after `post_start` work begins and before every assertion, restore it, then assert -- so the assertions describe what SURVIVED the interruption. See **`recover:`** below. Mutually exclusive with `expect_halt` (one expects RUNNING back, the other expects terminal), and requires an assertion beyond `smoke` (which only re-checks RUNNING, something the phase already waits for). |
| `exact` | mapping | no | none | turns on exact comparison for the `data`, `diff` and `file` specs that carry their own `exact:`: every row counted, values compared by declared type. See **`exact:`** below |
| `audit` | mapping | no | none | `{version: 1}` enables audited exact specs; each names its append writer, and Spanner also names its sink-generated delivery key |
| `transactions` | list | no | `[]` | ordered Postgres session steps; see `transactions:` below |
| `snapshot_observer` | mapping | no | none | concurrent consistent target reads checked against declared committed prefixes |
| `lifecycle` | mapping | no | none | how the framework knows the app is ready and has finished, before any assertion runs, and that the run cleans up only what it created. See **`lifecycle:`** below |
| `depth` | string | no | `regression` | one of `smoke`, `gate`, `regression`, `fault`, `customer`, `measure`, `canary`; becomes the pytest marker `depth_<value>`, for selecting with `-m` |
| `diff_poll` | number | no | `2` | seconds between the reads of an `assert.diff` poll; must be greater than 0 |
| `generate` | list | no | `[]` | run a data generator and place its output on the Striim server: entries `{kind, workload, dest, when}`. `kind` is `ggtrail` (GoldenGate trail files from a `workload.yaml`); `workload` is relative to the test dir; `dest` is a server directory; `when` defaults to `post_start` |

`placement` is also a valid `xfail.tiers` entry. It is excluded from the default
tiers and must be explicitly opted into: a failure to prove placement must not be
absorbed by an unrelated expected data failure. The same smoke, deployment and
provisioning exclusions described in the table still apply.

## `audit:` — append-only delivery targets

An upserting business table can hide a replayed delivery. Add a separate audit
branch that records deliveries and compare its hand-written golden as a counted
multiset. Enable the top-level `audit: {version: 1}` block, then add
`audit: {writer: '${TID}writer'}` to each audited `assert.data` or `assert.file`
spec. Each spec also requires canonical `exact:` and `order: any` (the default),
and lifecycle completion must observe that audited output. A vacuous block, unknown keys or an expected
halt are refused (`invalid-audit-spec`). The top-level key makes older framework
versions refuse the feature instead of silently treating a merge target as an audit.

The named writer must be declared in the app and write to that exact table, topic
or file. Exception handlers and automatic resume are refused, so a writer failure
cannot be swallowed. All routes supported by exact data/file comparison are supported:

- Postgres: a keyless table, with no unique/exclusion index, user trigger or
  rewrite rule. The framework checks that shape and installs an owned trigger
  that rejects UPDATE, DELETE and TRUNCATE with an error. The guard function is
  acquired and confirmed in the ledger, checked by definition hash before
  comparison, and dropped after the audit table at cleanup. Normal writer DML
  permissions remain available. Checks run before deploy and before comparison.
- Spanner: `audit: {writer: '${TID}writer', delivery_key: delivery_id}` requires
  a sole primary key with a UUID default (`GENERATE_UUID()` in GoogleSQL,
  `spanner.generate_uuid()` in PostgreSQL dialect). That key must be absent from
  the locally typed writer input and explicitly listed in `exact.ignore`; custom
  `KeyColumns` are refused. Each delivery gets a fresh sink-generated identity,
  while equal business rows are all counted. Business keys cannot absorb replay.
  In audit cases, Spanner also supports positive lifecycle baseline/source-count
  and row-count probes, on owned tables. No empty count can prove completion.
- Kafka: `project: kafka_record`; the owned audit topic must have `cleanup.policy:
  delete`, `retention.ms: -1` and `retention.bytes: -1`. Framework-created target
  topics in audit cases get those settings; compaction or bounded retention is
  refused. Every decoded frame is counted, even for equal keys and values.
- File: the matching FileWriter output under the owned directory, read with the
  existing exact file reader. All matching rolled parts participate in the multiset.

Database audit writers require a locally declared typed input stream, explicit
`ExecuteInsertAsUpdate: false` and `ExecuteUpdateAsInsert: false`, and no ignored
database errors (`audit-writer-not-append-only`). Convert CDC operations to typed
delivery records in a CQ rather than applying updates/deletes to the audit table.
Writer bindings are compared as written, before token rendering. Use the same
tokens in TQL and the assertion; a literal spelling that happens to render to
the same name is refused. Only one output binding is allowed.

| Exact route | Required adapter | Output property |
|---|---|---|
| `postgres-source`, `postgres-target` | `DatabaseWriter` | `Tables` equals `target` |
| `spanner-google` | `SpannerWriter` | `Tables` equals `target`, or `${SPANNER_GSQL_DB}.` followed by that exact target |
| `spanner-postgres` | `SpannerPGDialectWriter` | `Tables` equals `target` |
| Kafka `project: kafka_record` | `KafkaWriter` (including versioned variants) | `Topic` equals `target`; compatibility `Topics` accepted, both refused |
| File | `FileWriter` | `FileName` equals `path`; exact reads include the matching rolled parts |

All database adapters require the locally declared typed input and false upsert
flags above; Kafka/FileWriter do not require those database-only properties.
A writer may have an optional `VERSION` clause. Ambiguous declarations, duplicate
properties and source-to-target `Tables` mappings are refused for an audit
writer. The GoogleSQL qualified alternative accepts only the selected
`${SPANNER_GSQL_DB}` token; it never strips an arbitrary database qualifier. Use a CQ to project onto its typed stream.

The database audit input is insert-only, so it records a CDC delete as a new
  typed delivery row rather than removing an earlier audit row. For append
  sentinels on files or Kafka, include marker records in the golden as well.
  After a successful phase the framework exports `${SENTINEL_READY_ID}` /
  `${SENTINEL_DONE_ID}` and the corresponding `_TAG` tokens to later seeds and
  goldens. Their bindings are recorded in evidence, so marker records can be
  derived from the sentinel SQL without copying observed output.

The business target can keep its merge semantics; the audit branch records each
delivery independently. `audit-target-not-owned` refuses foreign outputs;
`audit-target-not-append-only` refuses unsafe storage. Duplicate rows then fail
the existing exact multiset comparison. A Spanner duplicate-key write must halt,
and an audit case cannot turn that halt into an expected success. The contract
does not claim delivery counts for a business target without an audit branch.

### Audit bindings on other routes

These fragments show the audited assertion; combine each with a completion
probe observing the same output, using the lifecycle sentinel forms below.
Goldens describe deliveries by hand, including any emitted marker rows. An ignored
generated delivery identity permits every repeated business row to be counted.

<!-- snippet: shape partial audit assertion; requires the matching TQL writer and lifecycle described here -->
```yaml
audit: {version: 1}
exact: {version: 1}
assert:
  data:
    - db: spanner-google
      target: '${TID}audit'
      match: expected/deliveries.csv
      exact:
        columns: {id: integer, msg: text}
        ignore: [delivery_id]
      audit: {writer: '${TID}writer', delivery_key: delivery_id}
```

Declare a local `${TID}row` type with `id` and `msg` and a `${TID}log` stream
of that type. Its writer uses `Global.SpannerWriter`,
`Tables: '${SPANNER_GSQL_DB}.${TID}audit'`,
`ExecuteInsertAsUpdate: false` and `ExecuteUpdateAsInsert: false`.
The table declares `delivery_id STRING(36) DEFAULT (GENERATE_UUID())` as its sole
primary key; do not put that field in the input stream. PostgreSQL-dialect
Spanner uses `Global.SpannerPGDialectWriter`, an identical `Tables`/`target`
spelling and the `spanner.generate_uuid()` default. In an audit case, positive
Spanner lifecycle counts are allowed; zero rows still cannot prove completion.

<!-- snippet: shape partial audit assertion; requires the matching TQL writer and lifecycle described here -->
```yaml
audit: {version: 1}
exact: {version: 1}
assert:
  data:
    - db: kafka
      target: '${KAFKA_TGT_TOPIC}'
      project: kafka_record
      keys: [value.id, value.msg]
      match: expected/deliveries.csv
      exact: {columns: {value.id: integer, value.msg: text}}
      audit: {writer: '${TID}writer'}
```

The matching KafkaWriter has `Topic: '${KAFKA_TGT_TOPIC}'`. Use the declared
lifecycle sentinel to acquire this fresh framework topic. The golden header is
`value.id,value.msg`; two identical source deliveries require two identical
rows, regardless of the Kafka record key.

<!-- snippet: shape partial audit assertion; requires the matching TQL writer and lifecycle described here -->
```yaml
audit: {version: 1}
exact: {version: 1}
assert:
  file:
    - path: '${OWNED_DIR}/rows.json'
      match: expected/deliveries.csv
      payload: json
      exact: {strict_json: true, columns: {id: integer, msg: text}}
      audit: {writer: '${TID}writer'}
```

The FileWriter has `FileName: '${OWNED_DIR}/rows.json'` and a JSON formatter
emitting the declared fields. Exact file reads expand the base name to its
rolled parts; raw inventory assertions instead name every physical file.
The golden `id,msg` contains one row per data or marker delivery. Adding a
business merge target alongside these audit branches does not change that count.

## `snapshot_observer:` — committed transaction prefixes during the run

The optional `snapshot_observer:` mapping starts a concurrent reader after
lifecycle readiness and before post-start input. It stops after completion,
then takes a final snapshot. The case must have `lifecycle:`. Each read observes
all named target tables in one transaction: PostgreSQL repeatable read/read only,
or one multi-use Spanner read-only snapshot. All tables must be confirmed resources
of this attempt. This observer never checks tables in separate transactions.

Required keys are `db` (an exact database route), `tables` (alias to a mapping
with required `target` and `columns`, plus optional `keys`) and `prefixes` (a non-empty list). Each prefix
maps every alias to a hand-written case-relative CSV golden. Declare every
compared column, using the exact type vocabulary. Every complete snapshot must
equal one complete prefix, with row multiplicity intact; matching each table to
a different prefix is a failure. The final snapshot must equal the last prefix.
Targets contain `${TID}` and two aliases cannot render to the same table.
When several prefixes declare the same state, intermediate reads choose the
earliest matching index compatible with the observed history. The final read
chooses the latest compatible match, so a repeated final state can complete.
This proves a possible monotone history; it does not prove every intermediate
prefix was observed.

Optional `interval` is a positive duration (default 100ms). `max_rows` caps total
rows in a read (default 100000, maximum 1000000); `max_bytes` bounds each table's
read and canonical comparison (default 64 MiB, maximum 512 MiB). `max_snapshots`
caps evidence history (default 10000, maximum 100000); reaching it fails instead
of silently discarding reads. The case timeout bounds a snapshot read.

Unknown keys, invalid routes, types, durations, alias sets or golden paths fail
with `invalid-snapshot-spec`. Runtime failures include `snapshot-target-not-owned`,
`snapshot-not-committed-prefix`, `snapshot-undeclared-column`,
`snapshot-table-set-mismatch`, `snapshot-target-ambiguous`, `snapshot-duplicate-column`,
`snapshot-limit-exceeded`, `snapshot-read-error`, `snapshot-read-timeout` and
`snapshot-final-prefix-missing`, `snapshot-prefix-regressed` and
`observer-incomplete`. This is an assertion record of type `data` (`xfail.tiers: [data]` addresses
all observer failures, including read failures; there is no snapshot tier). The record includes every
observation's prefix index, row count and combined canonical hash. Goldens are
read from the case's input snapshot; the framework never derives them from output.

Each table's optional `keys` is a non-empty list of unique declared column names,
used only for diagnostics; it defaults to all declared columns. Prefix comparison
always checks all declared columns. An undeclared state records `tables` per alias:
`keyColumns`, canonical `keys` arrays, total `rows`, `keysTruncated` and
`valuesTruncated`. It retains the first 50 rows per table and at most 256 characters
per value, including when an earlier failure abandons the observer. Null remains
null; other values use the exact canonical type representation.

A CDC sample that promises atomic target transactions must pass source transaction
boundaries and enable the writer's transaction preservation. Sample 12 uses
`FilterTransactionBoundaries: false` and `PreserveSourceTransactionBoundary: true`;
without these, a partial transaction can be expected behavior.

Sampling checks every observed snapshot. It cannot rule out a bad state between
reads. Choose an interval suitable for the behavior under test. The observer is
joined before cleanup, including when seeding or recovery fails.

Snapshot histories include both monotonic time and `observedUtc` so they can be
aligned with transaction steps. Worker failures are reported immediately. If a
case fails before observer assertions, `Observer.abandon()` retains the bounded
history in a failed `observer-incomplete` record. A finished observer is not
reported incomplete during teardown; the original case failure is preserved.


## `transactions:` — overlapping Postgres seed sessions

The optional ordered `transactions:` list controls named seed sessions. Session
names match `[A-Za-z_][A-Za-z0-9_]{0,62}`; they are not SQL connection strings. Each
step has `session` and `op`, optional `when` (`pre_deploy`, `post_start` (default)
or `post_recover`), and optional `after` (a duration from the start of that
transaction phase). The phases must occur in that order; steps within a phase
execute in list order. Transaction steps run before ordinary seed files during
pre-deploy and post-recovery; post-start ordinary seeds run first. Post-start
steps require lifecycle readiness. A begin can add `observe` for bounded commit
proof (see Transaction source diagnostics below);
post-recover steps require `recover:` and run after the last restore, before settle.

Every transaction step writes an assertion record of type `transaction` to the
results sidecar and evidence, and a start/end progress-log line. The actual row
contains session, op, phase, UTC startedAt/endedAt, durationS and succeeded/failed
outcome, including commits after recovery. This proves the framework executed a
commit; it does not prove the product reader captured it. Expected target rows
remain the test's independent check. A failed step retains its failure record
before owned sessions are rolled back and closed.

- `op: begin` requires `db: postgres-source` or `postgres-target` and opens a new
  owned connection. Begin two sessions before committing either to overlap them.
- `op: sql` requires `file`, read and token-rendered like a seed file. It executes
  in that session's open transaction. Transaction-control statements in these
  files are refused (`transaction-control-in-sql`); use explicit steps.
- `op: savepoint`, `rollback_to` or `release` requires a plain identifier `name`.
  Rollback-to keeps its savepoint and removes later savepoints; release removes
  it and later savepoints. Rolled-back SQL remains in the input evidence.
- `op: commit` or `rollback` closes that session. List commits in the desired
  order. A session can stay open across steps and across `recover:` until a
  post-recover commit or rollback.

Unknown keys, invalid operations, routes, paths or session/savepoint state are
`invalid-transaction-seed`. Every declared session must eventually close. The
framework rolls back and closes all remaining sessions on any failure, before
table teardown, even when resources are kept. Connections and SQL statements
use the case timeout. No connection is shared with another case. Pre-deploy
steps cannot have a non-zero delay.

<!-- snippet: fragment -->
```yaml
transactions:
  - {session: a, op: begin, db: postgres-source, when: pre_deploy}
  - {session: a, op: rollback, when: pre_deploy}
```

### Transaction source diagnostics

Add `observe` to a transaction `begin` to capture source proof on its commit.
It declares 1..16 table/key pairs and an optional owned replication slot.
Unknown keys are refused with `invalid-transaction-observe`; unowned runtime
references fail `transaction-observe-not-owned`. Each commit captures
`txid_current()` on its session before commit, then confirms `txid_status()`
through a separate bounded probe and records the declared source keys. An
unconfirmed commit fails `transaction-commit-not-confirmed`. Post-commit probe
failures retain `commitSucceeded: true` while reporting failed evidence.

<!-- snippet: fragment -->
```yaml
lifecycle:
  version: 1
  mode: cdc
  sink: db
  readiness: {kind: source-progress, db: postgres-source}
  completion: {kind: row-count, db: postgres-source, table: '${PG_SOURCE_SCHEMA}.${TID}src', expect: 1}
  reset: owned
transactions:
  - session: a
    op: begin
    db: postgres-source
    observe:
      tables:
        - {table: '${PG_SOURCE_SCHEMA}.${TID}src', key: id}
      slot: '${PG_SLOT}'
  - {session: a, op: sql, file: changes.sql}
  - {session: a, op: commit}
```

Source observations use separate connections and do not commit or close held
seed sessions. Recovery records source keys and the slot's confirmed_flush_lsn
and restart_lsn before interruption and after restore, with UTC times. These
probes have a two-second budget. Key samples select at most 51 values on the
server, retain 50, and bound each value to 256 characters, with explicit
truncation flags. Diagnostics do not replace the target's exact assertion.

For `lifecycle.completion.kind: row-count`, optional `key: id` captures a bounded
sample of target keys on failure using a separate two-second diagnostic probe.
It cannot satisfy or extend the completion deadline. Probe errors are retained
alongside the original failure. Lifecycle evidence keeps the first 10 and last
40 observations within its existing 50-observation cap.


### Oracle observer DDL and seeds

With `requires: [oracle]`, use `db: oracle-source` or `oracle-target` for the
corresponding data user, and `db: oracle-admin` for a privileged observer trigger
owner. The admin route uses `SLT_ORA_ADMIN_USER` / `SLT_ORA_ADMIN_PASSWORD`
(default `system` / `striim`) and the same data PDB, `SLT_ORA_SERVICE`.
It never uses the CDC root service. An unused admin route opens no connection.

<!-- snippet: fragment -->
```yaml
requires: [oracle]
ddl:
- file: observer_tables.sql
  db: oracle-target
- file: observer_trigger.sql
  db: oracle-admin
```

The same routes are accepted in `seed` entries with the normal `when` and `after`
keys. Put tables, views and grants before the trigger that uses them. Use `${TID_ORACLE}` for
observer view and trigger names. The `oracle-admin` route accepts only view/trigger
CREATE and DROP statements; other SQL fails `oracle-observer-ddl-unsupported`
before the script opens a connection. Run tables, seeds and grants on the data-user
routes. On `oracle-admin`, observer objects
must belong to that route user and carry this case's prefix; foreign names fail
`oracle-observer-ddl-scope` before execution. `DROP VIEW IF EXISTS` checks the
actual object name. Materialized views on this observer route are refused with
`oracle-observer-ddl-unsupported`. Ordinary `oracle-source`/`oracle-target` SQL,
including existing unprefixed views, keeps its existing naming contract. Trigger bodies keep their internal
semicolons and end with a slash on a line by itself, as in SQL*Plus; a missing
slash fails `oracle-plsql-terminator`. Plain SQL keeps its existing semicolon
format. Alternative `q'...'` quoting is refused with `oracle-sql-quoting`; use
doubled single quotes instead. This extension supports trigger definitions,
not general anonymous PL/SQL scripts.

Ordinary data-user DDL and seeds create no observer cleanup obligation. Observer
cleanup runs only after case-prefixed views/triggers or use of `oracle-admin`.
A case without observers does not read their catalogs or fail on such a read.

Created observer triggers are persisted in the ownership ledger: an absence check
and intended entry precede CREATE, and successful CREATE confirms the entry.
Cleanup and `python -m livetest.ownership replay <ledger.json>` delete triggers
with intended or confirmed ownership by exact name on the recorded user, endpoint,
CDB DBID, PDB name, PDB DBID and GUID. The PDB identifiers come from
`SYS.V_$CONTAINERS` for the current `USERENV.CON_ID` and are checked before every
drop and absence verification. A same-name replacement PDB is refused.
An intended trigger is eligible because absence was
checked before CREATE: Oracle may commit DDL and then lose the response. Old
`unresolved-intent` trigger entries are also replayable with these checks, only
when they already record both PDB identifiers. Missing identity fields fail
`oracle-observer-pdb-identity-missing`, with manual cleanup instructions: an
administrator must verify the original PDB and ownership before running the
reported `DROP TRIGGER "owner"."name"` there. Never infer missing fields from the
current same-name PDB.
An existing trigger without a matching ownership entry is preserved and refused with
`oracle-observer-not-owned`; an unreadable or changed target is not deletion
authority. Passwords come from current settings and are never stored in the ledger.

After a case, the framework removes its owned database triggers first,
then its prefixed views, before ordinary Oracle table cleanup. This also runs
after DDL, deploy or assertion failure, for seed-only routes, and with either
resource-retention flag: retained apps/tables do not retain observer hooks.
This also removes existing case-prefixed observer views when resources are kept;
unprefixed ordinary data-user views are outside this observer cleanup.
Oracle observer views use the case prefix as deletion authority, including a
pre-existing view carrying that prefix; they are not recorded in the ledger. This
differs from Postgres views, which require an exact ownership-ledger entry. Keep
Oracle observer prefixes reserved for this run and case.
Catalog readback verifies absence. A failed drop or unreadable catalog fails
cleanup as `oracle-observer-cleanup-failed`, preserves Oracle tables and records
verification gaps for their skipped cleanup, keeping any original case failure
visible. Observer teardown runs before app cleanup so a slow undeploy cannot skip
it when an interrupt teardown deadline expires. An admin-route observer compilation warning fails as
`oracle-ddl-compilation-error` after an attempt to drop the just-created object. An empty cleanup prefix is refused as
`oracle-observer-cleanup-scope`; unrelated objects are untouched.

Probe connections identify themselves as `PROGRAM = 'striim-test-observer'`
during login, including data assertions and admin DDL. Observer queries/triggers
must exclude that program while filtering the writer's username. Do not filter
on MODULE set after connect: an AFTER LOGON trigger has already run by then.
The supported Thin-mode driver minimum is python-oracledb 2.5.

Observer views and triggers see every session of the data user in the PDB,
including other cases and runs. Exact counts are valid only in a serial run on an
unshared service. Use `disabled_parallel` with that reason; this guard does not
isolate separate runs that share a service. The shipped regression records all
target logons and checks PROBES and WRITERS with `min_rows: 1`, proving both that
the framework tag reaches Oracle and that the writer remains visible.

See [Oracle observer setup and SQL](SERVICES.md#oracle-observers) for direct
grants, a filtered view/logon trigger, and the required image rebuild.

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

A `post_start` seed with no `after` runs no earlier than 5 s after RUNNING (`SEED_FLOOR_SECONDS`)
when the source's reader cannot report that it is reading and no `lifecycle:` proved readiness:
RUNNING is not reading, and an Oracle seed committed 1 s after RUNNING was measured missed. A
source whose driver waits for its reader, or a `lifecycle:` case, seeds without the floor; an
explicit `after` replaces it.

### Postgres fixture SQL deadlines

Postgres fixture connections retain cold-start retry (up to 30 attempts, two seconds
between attempts), with `connect_timeout` set to five seconds per attempt.
Statement and lock deadlines are **opt-in** for both `ddl:` (including replication-slot
setup) and seed SQL, including action seeds. With neither a manifest budget nor a
setting, the framework sends no statement/lock timeout options and preserves server
policy. Existing bulk-load fixtures therefore do not acquire new cancellation limits.

Set `fixture_timeout: 1800` at the top level to opt in for one case, or set
`SLT_PG_FIXTURE_TIMEOUT=1800` in the shell, `.env` or machine settings for a host-wide
live-engine default. Precedence is manifest > setting > none. Values are positive,
finite seconds, at most `2147483.647`, independently of assertion `timeout`. An enabled
budget sets the statement deadline and a lock deadline of min(15 seconds, half the
statement budget); leave both inputs unset to retain longer legitimate lock waits.
Concurrent `action:` DML loops keep their previous unbounded SQL path and do not start
fixture observers.

`postgres-fixture-timeout` reports the phase, route, and observed blockers: backend PID,
application name, transaction age, and the owning run when its namespace is recorded in a
ledger, with the observation time. Passive diagnostics stay enabled even without a
fixture deadline. Observation starts after two seconds and is limited
to three snapshots, five seconds apart. Each diagnostic has a two-second connection limit
and a one-second statement deadline. If a diagnostic outlives successful SQL, a
`postgres-fixture-diagnostic-gap` warning records the missing observation; committed SQL
remains successful. The fixture
disconnects its own SQL session; the blocker remains running. Run correlation is diagnostic
information, not cleanup authority.

The [PostgreSQL 16 statement deadline](https://www.postgresql.org/docs/16/runtime-config-client.html#GUC-STATEMENT-TIMEOUT)
applies separately to each statement in a multi-statement SQL file. It does not replace the
whole-case watchdog. Explicit transaction statements are sent as written.
Connection limits apply per address, as specified by
[libpq](https://www.postgresql.org/docs/16/libpq-connect.html#LIBPQ-CONNECT-CONNECT-TIMEOUT).

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



Samples 11 and 12 enable recovery, wait for B to reach the target before STOP
with a 0 < count < 2 progress window, and prove a fresh post-recovery sentinel
before committing held session A. Sample 12 uses separate marker tables on
the same reader/writer so sentinel traffic does not alter snapshot prefixes.
Their hand-derived expected rows are unchanged.

## `recover:` — interrupt the app, bring it back, then assert

Every other phase verifies a pipeline that ran undisturbed. That cannot observe a whole class of
defect: an app checkpoint claiming progress no target durably made, so the events
in flight at the interruption are never replayed. `recover:` is how a test asks for that.

| Recovery key | Default and constraints |
|---|---|
| `mode` | Required: `stop`, `quiesce`, `kill`, or `restart` (the last requires `node`) |
| `after`, `every` | Timed delays; both refused with `trigger`; `every` needs `times > 1` |
| `times` | 1; repeated cycles require `expect_running: true` and a repeatable trigger |
| `settle` | 20s after the last restore |
| `expect_running` | `true`; a recovery marker requires it |
| `trigger` | Optional bounded progress reader, defined below |
| `sentinel` | Optional `true`, with the declared lifecycle sentinel |
| `node` | Optional exact container role or hosting-flow selector; unavailable with `quiesce` |

<!-- snippet: fragment -->
```yaml
recover:
  mode: stop          # required: kill | stop | quiesce | restart
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

**Modes without `node:` operate on the application or the app-node cluster:**

| mode | What it does | Notes |
|---|---|---|
| `kill` | `docker kill -s KILL` the app node containers, then `docker restart` + re-auth + wait ready | The crash case: no `close()`, no flush, no rollback. **Docker only** — the test SKIPS on a native Striim rather than downgrading to `stop`, which exercises a different path and would report a pass for a case never run. |
| `stop` | `STOP APPLICATION`, then `START` | Interrupts the app without requesting a drain. Pending writer batches can be rolled back. |
| `quiesce` | `QUIESCE APPLICATION`, then `START` | Requests a flush and checkpoint before stopping. Use as a control for a shutdown-loss test. |
| `restart` | Requires `node:` | There is no application-level restart mode. |

`stop` and `quiesce` differ by exactly one platform behaviour, which makes them a matched pair —
run the same manifest under both.

> **⚠ `kill` without `node:` affects the app-node cluster.** It SIGKILLs every container in
> `stack.app_nodes()`, so under `SLT_PARALLEL`/xdist **every other in-flight test on those nodes
> dies with it**. Nothing enforces this today: pair a `kill` manifest with
> `disabled_parallel: true`, or keep it out of concurrent runs. `stop` and `quiesce` are
> app-scoped when `node:` is absent.

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
pass and would prove nothing. To prove the restart replayed a given seed, add
[`checkpoint_history: {predates: <seed>}`](#predates--prove-a-recover-case-replayed-a-seed).

Example: `scripts/live/regression/framework/framework-recover`, a writer on a tight commit policy
that survives a `STOP`.

### Single-node faults and flow placement

Named-node recovery requires Docker mode, `topology: cluster`, the framework's
primary and secondary app-node containers plus its agent, and an unambiguous
Striim server-name mapping to each running container. A native case is skipped.
The survivor must publish its REST API port. Sample 17 kills the secondary and
uses the primary API (default 9080); it does not require the secondary API port.
[UPGRADING.md](UPGRADING.md) applies only when the secondary must serve fallback reads.

| Key or combination | Meaning and requirements |
|---|---|
| `node: primary` | Select the primary role; refused when it hosts metadata, as in the bundled recipe |
| `node: secondary`, `agent` | Select that exact framework container role; this alone makes no claim about flow ownership |
| `node: {hosting: '${NS}.${TID}flow'}` | Requires one running Docker owner; deploy the flow `ON ONE IN ${TID}workers` with the owned secondary group below |
| `mode: kill` with `node` | `docker kill -s KILL` only the selected container ID |
| `mode: stop` with `node` | `docker stop` that container, rather than `STOP APPLICATION` |
| `mode: restart` | `docker restart` that container; requires `node` |
| `mode: quiesce` with `node` | Refused; quiesce is application-level |
| `disabled_parallel: true` | Excludes the case from concurrent selection; serial runs still execute it and it does not lock the shared stack |

When proving worker failover, the metadata repository must remain available.
The bundled recipe hosts it in the primary container. A node fault selecting
that metadata host is refused before interruption with
`fault-node-hosts-metadata`. Use the secondary as the initial worker.

### `deployment_groups` — owned initial placement and failover members

Docker mode can create case-owned global deployment groups before importing TQL.
Names must start with `${TID}`; `flow` must name a flow in `${NS}`. The framework
refuses collisions, records acquisition in the ownership ledger, verifies exact
membership, and drops these groups after removing the owned namespace. Cleanup
and saved-ledger replay verify absence on the recorded Striim endpoint.

<!-- snippet: fragment samples/live/17-one-node-failover/test.yaml -->
```yaml
deployment_groups:
- name: ${TID}workers
  flow: ${NS}.${TID}flow
  servers:
  - ${STRIIM_SECONDARY_SERVER}
  add_after_deploy:
  - ${STRIIM_PRIMARY_SERVER}
```

Deploy the named flow `ON ONE IN ${TID}workers` in the app's TQL. Before the
post-start seed, the framework proves that the flow has one running owner among
`servers`, then adds `add_after_deploy` members and verifies the complete group.
The existing singleton flow keeps its initial worker; the added member becomes
eligible for failover. This is for `ON ONE` flows; it does not expand an existing
`ON ALL` deployment onto new servers. Sample 17 proves movement to the added
primary while the secondary stays down.

Only `name`, `flow`, `servers` and optional `add_after_deploy` are accepted.
`servers` is nonempty; both member lists contain distinct framework server
tokens, and agents cannot mix with app servers. Malformed declarations fail
`invalid-deployment-group`; unreadable catalogs fail
`deployment-group-catalog-unparseable`; existing names fail
`collision:deployment-group`; changed membership fails
`deployment-group-membership`; wrong initial owners fail
`deployment-group-initial-placement`. The initial owner proof retries read errors
under the case timeout and records its allowed/observed owners, last read error
and bounded native replies as a `recovery` record with `phase: initial-placement`,
on success or failure. Native cases skip before inventory or group creation.
No foreign group is modified.

Use `{hosting:}` to prove the fault hits the running worker. A named-role fault
tests that role, and `assert.placement` separately checks resulting flow placement.
Coordinate all users of these shared nodes before running a node fault. A node
operation affects every application hosted there, even when it selects one flow's
owner. Resolve the owner before waiting for the progress window; the final
pre-command check revalidates only that owner and MON's singleton count, leaving
one fresh progress check immediately before the command.

In Docker mode, add `recover.node` to scope `stop`, `kill` or `restart` to
one container. Use `primary`, `secondary`, `agent`, or `{hosting: named flow}`.
The hosting flow must belong to this case's namespace and have exactly one
running owner in current Striim deployment metadata. A flow MON identifies the
flow; it does not provide application status. Server DESCRIBE binds that flow to
one full parent-application name, and STATUS on that same server must report the
application RUNNING. Missing identity, parent or runtime state fails with
`placement-flow-monitor-unavailable`, `placement-application-unavailable` or
`placement-node-status-unavailable`; a deployed but stopped app is not a running
owner. Its owner is checked again
before the final progress read and fault command. Ambiguous selectors and unknown
keys fail with `invalid-node-fault`; a moved or nonunique flow fails `fault-missed`.
A refused hosting selection or changed owner records a failed `recovery` assertion
before raising. Its row includes the phase (`owner-selection` or `fault-boundary`),
flow, observed owners, selected server when available, Docker server/container
mapping, `interrupted: false` and placement `reads`. Those reads preserve the
returned deployment-group, MON, queried per-server DESCRIBE and runtime STATUS
values as redacted
JSON strings, with original redacted character counts and truncation flags.
Each response is capped at 65536 characters; a truncated string is not complete
JSON. A read error retains the responses already obtained and reports owners as
null rather than inventing an empty owner set. These are diagnostics only: zero,
multiple or unmapped owners still prevent a fault.

`quiesce` cannot target a node. `restart` requires a node. Native mode skips named
node faults, and continues to skip legacy kill.

<!-- snippet: fragment -->
```yaml
topology: cluster
disabled_parallel: true
deployment_groups:
- name: ${TID}workers
  flow: ${NS}.${TID}flow
  servers: ['${STRIIM_SECONDARY_SERVER}']
  add_after_deploy: ['${STRIIM_PRIMARY_SERVER}']
recover:
  mode: kill
  node: {hosting: '${NS}.${TID}flow'}
  trigger:
    kind: target-count
    db: postgres-target
    table: '${PG_TARGET_SCHEMA}.${TID}tgt'
    above: 100
    below: 2500
assert:
  placement:
    - {flow: '${NS}.${TID}flow', node: survivor, timeout: 90s}
  data:
    - {target: '${PG_TARGET_SCHEMA}.${TID}tgt', db: postgres-target, rows: 5000}
```

Docker commands use the captured container ID and verify its resulting running
state. A failed command fails `node-fault-error`. Only that container is started
again, including after a failed case. A hosted app-node flow must move to the
other app node while its owner is stopped; missing failover fails
`fault-node-no-failover` under the restore deadline. A separate `recovery` assertion
with `phase: failover-wait` records success or failure before restarting the
original owner. Each placement read records its index, start/observation times,
UTC time, remaining budget, owners or a redacted error, and whether it finished
late. A late survivor read cannot satisfy the deadline. History keeps at most
128 entries (the first 127 plus the latest); `observationCount` and
`observationsDropped` expose omitted middle reads. Error text is capped at 1024
characters with `errorTruncated`; `reads` preserves the last native placement
reply diagnostics under their existing per-response limits. The agent has no app-node
survivor requirement. Hosting faults require `times: 1`; after one worker
failover the next owner may be the protected metadata host. Repeated hosting
faults are refused at load as `invalid-node-fault`. Named-role faults may repeat
under the existing trigger/cycle rules.

All `recover.node` cases require `SLT_ENABLE_NODE_FAULTS=1`, set only for a
dedicated disposable stack. Without it they skip before provisioning even when
selected by a default samples run. `disabled_parallel` does not replace this gate.
Only opted-in stacks add the secondary API port overlay.

The surviving app node's published API port is discovered by Docker inspection.
For the bundled secondary-worker fault, the primary API remains available at
its published port (default 9080). The second-node mapping, default 9082
(`SLT_STRIIM_NODE_HTTP_HOST_PORT`), is needed only when that node is the survivor.
Missing fallback endpoints fail `fault-fallback-unavailable` before interruption.
Read-only status, monitor, description and checkpoint reads share a bounded
budget with authentication to a surviving endpoint after connection, timeout or
endpoint authentication failures. State-changing API calls execute once.
Legacy recovery without `node` retains its existing behavior.

Docker topology resolution requires a fresh observation of at least two app
nodes in `default` and an agent in `Agents` after node join has settled. A
temporarily incomplete observation is retried within the original 300-second
join budget plus `SLT_CLUSTER_SETTLE` (default 20 seconds); it is not cached as
single-node after successful registration. Late reads cannot prove readiness.
Persistent missing membership skips the case with `CLUSTER-READY`, the
last group counts and saved node logs when the run has a JUnit output directory.
The cluster requirement of a `topology: cluster` case is unchanged.

`assert.placement` is a nonempty list of `flow`, `node`, and optional positive
`timeout` (default case timeout). Unknown keys fail `invalid-placement-spec`.
`flow` must name this case's qualified flow. `node` is an exact live Striim server
name, a Docker role above, or `survivor` (requires an app-node fault).
Placement failures require explicit `xfail.tiers: [placement]` to be expected;
string-form or untiered xfail does not absorb them.
The assertion retries read errors under its existing timeout and records the last
redacted error in `detail`, including when a later read succeeds.
The assertion reads running flow status and per-node deployed-flow metadata;
exactly the requested node must own the flow. Zero, multiple or wrong owners fail
`placement-mismatch`; unreadable metadata fails `placement-read-error`.
Placement records retain expected and actual nodes. See
`samples/live/17-one-node-failover` for a two-node case with complete data and a
post-recovery sentinel.

### Post-recovery route proof

| Recovery marker key | Contract |
|---|---|
| `sentinel` | Optional, absent by default; the only accepted value is `true` |
| `expect_running` | Must be `true` (the default) |
| `lifecycle.sentinel` | Must be declared, with source insert/delete SQL and an owned observable sink |
| Per-cycle marker | Fresh ID/tag after every restore, present then absent before subsequent work |

Set `recover.sentinel: true` to insert a fresh lifecycle marker after **every**
restore reaches RUNNING, before post-recovery transaction steps, ordinary seeds,
settling and assertions. It requires `expect_running: true` and a declared
`lifecycle.sentinel`; false, mappings and unknown values are refused with
`invalid-post-recovery-sentinel`. The existing Postgres-source SQL and all A4
sink kinds (Postgres, Spanner, file, Kafka) are reused with current-attempt
ownership and the lifecycle completion deadline. Database sinks observe present
then absent; append sinks observe insertion then its deletion event.

A new ID/tag is allocated per recovery cycle; `SENTINEL_RECOVERY_1_ID` and
`SENTINEL_RECOVERY_1_TAG` (then 2, 3, ...) become tokens and evidence bindings
after proof succeeds. The prefix is reserved. A recovery assertion record retains
its marker observations and real expected insert-present/delete-absent states,
so the complete written evidence validates without an inapplicable-field reason.
Missing, unreadable or terminal routes fail with
`post-recovery-sentinel-missed`, never as an expected data failure, even when the
final golden was already met. See `samples/live/16-recovery-sentinel` for a marker
suppression check. File reads still require Docker mode.

### Recovery progress triggers

Use `recover.trigger` instead of `after`/`every` to interrupt when progress is
observed. Each cycle waits afresh; timeout and poll are positive durations
(default 60s and 100ms). Unknown keys fail with `invalid-progress-trigger`.
Target tables, slots and topics must belong to the current attempt. Reads are
bounded by the remaining trigger deadline. A read finishing late cannot satisfy
the barrier. A terminal app, unreadable position, deadline or overshot window
fails the case before interruption. Timed `after:` recovery keeps its behavior.

<!-- snippet: fragment -->
```yaml
recover:
  mode: stop
  trigger:
    kind: target-count
    db: postgres-target
    table: '${PG_TARGET_SCHEMA}.${TID}tgt'
    above: 100
    below: 2500
    timeout: 90s
    poll: 100ms
assert:
  data:
    - {target: "${PG_TARGET_SCHEMA}.${TID}tgt", db: postgres-target, min_rows: 1}
```

Trigger kinds:

| kind | Fields and condition |
|---|---|
| `target-count` | `db`, `table`, integer `above`, `below`; count must be strictly between both bounds. Postgres and both Spanner dialects. |
| `checkpoint` | optional `field` (default serialNo), optional integer `above`; maximum history field must advance past above. Without above, the baseline is read before post-start input, using the existing checkpoint-history reader. Missing fields fail. |
| `kafka-end` | `topic`, integer `partition`, integer `above`; fresh topic end offset must exceed above. |
| `kafka-consumer` | same plus `group`; committed consumer offset must exceed above. A group without a committed offset has not passed the barrier. |
| `postgres-slot` | Postgres `db`, `slot`, `above` as hexadecimal LSN (for example 0/10); confirmed_flush_lsn must exceed it. Full 64-bit positions are compared. |

All triggers require `kind`; `timeout` defaults to 60s and `poll` to 100ms,
both positive finite durations. Unknown and kind-specific foreign keys are
refused with `invalid-progress-trigger`. `trigger` cannot coexist with `after`
or `every`; the loader refuses either combination. Absolute thresholds require
`times: 1`. A checkpoint trigger without `above` can repeat with an advancing
per-cycle baseline; repeated recovery requires `expect_running: true`.

| Kind | Required fields and constraints |
|---|---|
| `target-count` | `db`, `table`, nonnegative integer `above`, `below`; `below >= above + 2` leaves an integer inside the strict window |
| `checkpoint` | Optional `field` (default `serialNo`), one ASCII identifier starting with a letter; optional nonnegative integer `above`, otherwise advance beyond the captured baseline |
| `kafka-end` | `topic`, nonnegative integer `partition`, nonnegative integer `above` |
| `kafka-consumer` | The Kafka end fields plus nonempty, token-rendered `group`; reads committed group offsets |
| `postgres-slot` | Postgres `db`, token-rendered `slot` matching `[a-z0-9_]+` at runtime, and `above` as a Postgres LSN |

Table, slot and topic reads require confirmed ownership in this attempt.
Kafka triggers use framework-derived source/target topics acquired by a declared
lifecycle, rather than an unclaimed custom topic. A slot must already be recorded
as owned; naming a shared slot does not grant permission to observe it.
Only target-count recovery has reported live qualification (sample 14);
checkpoint, slot and Kafka position triggers have hermetic coverage.
Every cycle writes an assertion of type `recovery` in `evidence.json` and the
results sidecar. Its `spec.trigger` holds the trigger declaration. The actual row contains `cycle`, `baseline`,
`windowOpened` (the first in-window value and monotonic/UTC time), the final
pre-command `value`, `held`, monotonic `observedAt`/`issuedAt`, UTC `observedUtc`,
`deadline` and `interrupted`. Timed recovery uses `trigger.kind: timed` and checks
RUNNING at issuance. Progress recovery reads its precondition at the command boundary.

A fresh progress read immediately before the command must still qualify.
After interruption, another read has at most five seconds or the shorter trigger
timeout; `afterInterrupt` retains its observations. A target count reaching
`below` in that interval fails `fault-missed` even if the pre-command count held.
These separate reads do not claim an atomic snapshot at the physical kill instant.

A failure before issuance records `held: false`, `issuedAt: null` and
`interrupted: false`; no command was issued. A closed window after interruption
records the issue time, `interrupted: true` and the post-interruption value.
A command failure retains its held precondition but cannot pass the interruption.
`fault-missed` and `fault-interrupt-error` are framework recovery failures;
`xfail` cannot absorb them. Inspect the suffix (`progress-deadline`,
`progress-window-missed`, `progress-read-error`, `progress-app-not-running`) and
the retained evidence. See [15-fault-evidence](../samples/live/15-fault-evidence/README.md).

Kafka offsets describe the next record position; the trigger observes the named
partition, never a sum across partitions. Checkpoint advances are measured anew
in each cycle. See `samples/live/14-progress-recovery`.

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
| `concurrent` | as for `stop_start_cycle`: SQL loops on background threads for the whole action. Errors on a route of the stopped `service` are expected and do not fail the case; each loop must still succeed at least once |

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

## Bounded absence after a sentinel

`nothing: {window: 3s, where: {id: 1}}` on an `assert.data` or `assert.file`
spec requires that no matching row arrive during the entire positive window.
`where` is a non-empty mapping of identity field paths to scalar values; every
field must match with its token type intact. Missing identity fields or incompatible
token types fail rather than silently counting as non-matches. Use fixed identity
fields that the route emits on every checked event. For file events use paths such as `data.id`;
for Kafka records use `value.id`. Data specs name `target` and `db` (or
`target_db`); file specs name `path`. Data absence specs take only `target`, `db`/`target_db`, `nothing` and optional
`payload`; file absence specs take only `path` and `nothing`. `project` is refused
here: Kafka selectors already use full record paths. No content match/count/exact
key can coexist. Kafka may choose `payload: json` (default `avro`).

The case must have `lifecycle.completion.kind: sentinel`, and that sentinel must
observe the same output table, topic or file. Invalid combinations, unknown keys
and an invalid window are `invalid-nothing-spec`. At runtime absence is refused
unless completion actually succeeded (`nothing-sentinel-unproven`). A late
matching event fails with `nothing-arrived`; a read failure is `nothing-read-error`.
Each read has an independent positive `nothing.read_timeout` (default `5s`),
including a fresh read started at the window boundary. This avoids reducing the
final transport budget to a few milliseconds. Slow reads can extend observation
past the minimum window; an event visible on that final read also fails.
Selectors name the prohibited input explicitly, so append-sink sentinel records
remain visible and are not silently excluded. This is a bounded claim, not a
claim that nothing can ever arrive afterward.

The lifecycle sentinel source remains Postgres SQL. Its `observe` supports:

- Postgres: the existing `{db, table, key}` present-then-absent probe.
- Spanner: `{db: spanner-google}` or `spanner-postgres`, `table` containing
  `${TID}`, and `key`. Use `sink: db`. Tables are acquired from the catalog before
  DDL and confirmed afterward; exact reads can use the same confirmed tables.
- File: `sink: file`, with `observe: {path, key, deletion}`. The path is under
  `${OWNED_DIR}/`, the key is an event field path (for example `data.id`), and
  `deletion: {metadata.OperationName: DELETE}` identifies the delivered deletion.
- Kafka: `sink: kafka`, with `observe: {db: kafka, topic, key, deletion}` and
  optional `payload: avro` or `json`. Keys and deletion selectors use record paths
  rooted at key or value. Only freshly acquired framework topics qualify.

Append sinks require both insertion and deletion events for the fresh sentinel
id. An empty file/topic cannot prove deletion because insertion is observed
first. Their readiness is sentinel (or existing source-progress for Kafka), and
completion is sentinel. Unknown observe keys are refused. No sentinel rows are
removed from content assertions. Declare their expected records if also checking
the complete append output.

Spanner tables and Kafka topics are checked for collisions before creation and
deleted by exact confirmed name. Since these backends expose no immutable
incarnation id here, cleanup of their prior-attempt ledger entries is refused;
matching endpoint configuration alone never authorizes a replayed delete.
File sentinels require Docker-mode server file access. An unsupported route or
unowned output fails; it cannot turn the absence check green.

## Raw file bytes and rolled-file inventories

Use `assert.file[].raw` for byte-exact output. `path` names the base output file;
`raw.files` declares the complete inventory of its parent directory across all
app nodes. A name on two nodes, an extra file or a missing file fails. Hidden files and
empty rolled files are inventory entries, never filtered out. The
directory should hold only the output being tested. Docker mode is required
(`raw-file-route-unsupported`). `path` must be under `${OWNED_DIR}/` and the
runtime directory must belong to this attempt (`raw-file-target-not-owned`).

FileWriter's first rolled file inserts `.00` before the extension:
`FileName: rows.csv` produces `rows.00.csv`, not `rows.csv`. Inventory keys are
actual basenames and may contain framework tokens such as `${TID}`; names are
rendered before comparison. The `path` supplies the parent directory to inventory,
so it may name the base output or its first rolled file; it does not filter out
other directory entries.

Empty files are inventory entries too: an undeclared empty rolled file fails.
Declare an expected empty file with zero bytes, the SHA-256 of empty bytes and
zero records. Sample 09 instead uses an event-count-only rollover policy above
its two-row input, so a timer cannot create an empty extra file. Its typed CQ
and explicit formatter members select only the two data columns, avoiding
variable event metadata; its ordered query and CRLF formatter derive the same
17 expected bytes.

Each filename maps to `bytes` (non-negative integer) and `sha256` (`sha256:` plus
64 lowercase hex digits). Optional `records` counts records in that individual
file: `raw.format: lines` (default) counts LF-delimited records, including a final
unterminated record; `json` counts complete JSON objects with the strict parser.
CR bytes remain part of the hashed bytes. An inventory cannot be empty.
Optional `raw.max_bytes` caps total bytes (default 64 MiB, maximum 512 MiB).
Unknown keys, unsafe names and invalid counts or digests are
`invalid-raw-file-spec`. A raw spec takes only `path` and `raw`, without `exact`.

<!-- snippet: fragment -->
```yaml
assert:
  file:
    - path: ${OWNED_DIR}/rows.csv
      raw:
        format: lines
        files:
          rows.00.csv:
            bytes: 0
            sha256: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
            records: 0
```

Hashes and lengths are compared before text conversion. Failures report
`raw-file-inventory-mismatch`, `raw-file-mismatch`, `raw-file-limit-exceeded`
or `raw-file-invalid-json` in the file assertion record. With lifecycle
completion, the inventory is read once after completion; otherwise it is polled
until it matches or the case timeout expires. Derive the expected bytes and
counts from the input and formatter contract, never from captured output.

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

The local BigQuery service also supports `bigquery-target` reads. Use a fully qualified
`${BIGQUERY_PROJECT}.${BIGQUERY_DATASET}.${TID}table` and require `bigquery`.
The reader preserves case and scalar INT64, NUMERIC, TIMESTAMP, DATETIME, DATE, BYTES,
BOOL and STRING values. Nested/repeated fields, JSON, GEOGRAPHY and floating point fields
are refused, including on empty tables. Its REST transport caps response bytes before JSON
parsing, reads at most `max_rows + 1`, and stops after 128 result pages. Raw row bytes also
have a conservative transfer cap of twice `max_bytes`; canonical bytes retain the exact
`max_bytes` limit. A disposable reader process enforces the total deadline even when a server
trickles bytes. Assertion details retain query job identifiers. Cancellation is bounded and
best effort; it does not prove server rollback.

### `exact:` on a spec

`exact: true` takes every default. The mapping form:

| key | default | meaning |
|---|---|---|
| `columns` | `{}` | column name → type (table below). Values of a typed column compare by meaning, so `118.0` and `118.00` are equal as `decimal:2` |
| `strict_json` | `false` | preserve JSON token types and require a declaration for every compared column; file or Kafka only |
| `ignore` | `[]` | columns removed from both sides before comparing (a load timestamp, say). Each must exist on at least one side, and cannot also be typed |
| `order` | `any` | `any`: the rows compare as a multiset, in any order. `sequence`: also in the same order |
| `order_by` | none | the columns the target is read in order of. Required with `order: sequence` on database `data` and `diff`, refused on Kafka/file routes. The values must be unique per row (`order-not-total` if not) |

The spec's own `keys:` (a projection) still applies, before `ignore`.

**Types.** Every column the database returns as something other than text, a whole number, a
boolean or NULL must be typed, or the run stops with `undeclared-conversion:<column>:<type>`. Text
columns need no entry.

| type | golden cell | compared as |
|---|---|---|
| `integer` | `-12` | the number |
| `text` | any text, including an empty cell | a string value; strict payloads require a string token |
| `number` | `1.25` | a finite numeric value; quoted numbers and booleans fail |
| `decimal:<s>` (s 0 to 38) | `118.00` | the value at `s` places; a value that does not fit exactly is `lossy-decimal`, never rounded |
| `boolean` | `true` or `false` | the value |
| `timestamptz` | ISO 8601 with `Z` or `±hh:mm` (`2026-09-01T08:00:00+00:00`), up to nine fractional digits | the instant, in UTC, to the nanosecond |
| `timestamp` | ISO 8601 without an offset, up to nine fractional digits | the local date and time |
| `date` | `2026-09-01` | the date |
| `binary:hex`, `binary:base64` | the bytes in that encoding | the bytes |
| `json` | JSON text | the JSON, with keys sorted; numbers keep their exact text (`1.0` is not `1.00`) |

**Golden files.** A UTF-8 CSV without a byte-order mark, with a header row of unique names:
- `<null>` is NULL. An empty cell is an empty string: accepted by `text`, refused by numeric/date/binary/JSON declarations.
- `<absent>` (file and Kafka record specs only) is a column the record does not carry.
- Write the golden by hand from your data. The framework never writes one.

**Strict JSON payloads.** Set `exact.strict_json: true` on a file or Kafka record
spec. It is refused on database routes (`invalid-declaration`). Declare every
compared column, including text and null columns; `keys` and `ignore` still select
what is compared. An undeclared compared column fails with `undeclared-column`. A quoted integer
or another token/type mismatch fails `invalid-value:actual:<column>`; fix the
producer rather than changing the expected token type.
`text` requires a string token (including an empty string); `number` requires a
finite numeric token and compares its exact numeric value. Existing `integer`,
`boolean`, `decimal:N`, `json` and `binary:hex` / `binary:base64` declarations work.
A `json` column retains scalar token types: number 1 differs from string "1".
Byte values require `binary:hex` or `binary:base64`; an object identity string is
refused by those encodings. The golden stays a hand-written CSV; JSON columns
contain JSON text, and `<null>` still means a null column.

Strict file reads refuse duplicate object keys, non-finite constants, incomplete
JSON and non-object events (`invalid-json`). They accept arrays of event objects
and successive complete event objects or arrays. Kafka `project: kafka_record`
uses Avro values by default; add `payload: json` to decode each message value as
strict JSON. `payload` takes `avro` or `json` and requires `strict_json: true`
(`invalid-exact-spec`). Message keys are strict JSON too. Unknown `exact` keys
are refused; the strict flag must be boolean. Legacy declarations keep their
existing parsing and comparison behavior.

**Where it works.**
- `data` and `diff`: `postgres-source`, `postgres-target`, `spanner-google` and
  `spanner-postgres`. Spanner reads retain native types within a bounded read-only snapshot.
- `data` also supports `db: kafka` with `project: kafka_record` and `keys:` rooted at
  `key` or `value`. Declare types using those full paths (for example `value.id: integer`).
  Missing paths stay absent; null stays null. Every decoded Avro frame is counted.
  `order: sequence` follows consumed record order and needs no `order_by`; use one partition
  when the test requires a single total order. Wire bytes and decoded rows are bounded.
  An incomplete captured Kafka snapshot fails rather than returning a matching subset. Each partition is bounded by its captured high offset; later messages cannot hide unread captured rows. Missing or erroneous topic metadata is a read failure.
  Other routes are refused at load (`exact-route-unsupported`).
- `file`: Docker mode only; the file is read from the cluster's nodes. No matching output file fails
  (`exact-file-missing`), even with a header-only golden. With `order: sequence` the
  path must match exactly one file on one node (`order-source-ambiguous`).
- With a `lifecycle:` block, every table an exact spec reads (a diff's source too) must be one this
  test's `ddl:` created, and every file must be under `${OWNED_DIR}/` (`exact-target-not-owned`).

**Refused at load** (by `striim-test doctor --case`, and at the start of a run):
- a spec with `exact:` and no top-level block (`exact-block-missing`), or a block with no spec that
  uses it;
- `exact:` without `match:`, or together with `rows`, `min_rows` or `ordered`; `project` is allowed only for
  Kafka `data` with `project: kafka_record`;
- an unknown key or type, a column both typed and ignored;
- a `match:` path outside the test's folder.

**Reading the result.** Each comparison is in the run's `evidence.json` under `data.comparisons[]`:
`equal`, and `samples.missing` and `samples.extra`, the rows that differ (up to 20 each, with their
counts). With a `lifecycle:` block the target is read once, after completion. Without one, it is read
again until it matches or `timeout` runs out.

`diff: [{..., exact: true}]` without the top-level block keeps its older meaning: the source and target
rows compared as text, with their counts.

### Strict Kafka JSON example

A JSON key `{"id":1}` and value `{"id":1,"active":true,"note":null}` produce the
hand-derived header `key.id,value.id,value.active,value.note` and row
`1,1,true,<null>`. Every selected field is declared, including the nullable note.

<!-- snippet: fragment -->
```yaml
exact: {version: 1}
assert:
  data:
    - target: ${KAFKA_TGT_TOPIC}
      db: kafka
      project: kafka_record
      payload: json
      keys: [key.id, value.id, value.active, value.note]
      match: expected/messages.csv
      exact:
        strict_json: true
        columns:
          key.id: integer
          value.id: integer
          value.active: boolean
          value.note: text
```

Changing value.id to the string "1" must fail even though its printed characters
match. This Kafka example has hermetic coverage; sample 08 qualifies the file
route live, not Kafka JSON payloads.


Worked case: [08-strict-json](../samples/live/08-strict-json/README.md).

### Native values on Spanner and Kafka

The declaration follows the value returned by the reader, not the formatted text.

| Returned value | Declaration | Limit or refusal |
|---|---|---|
| Spanner INT64 / Avro int or long | `integer` | booleans and quoted numbers are not integers |
| Spanner NUMERIC / Avro logical decimal | `decimal:<s>` | choose the required scale; any value needing rounding fails |
| Spanner FLOAT64 / Avro float or double | `number`, or `decimal:<s>` when exactly representable at that scale | non-finite values and lossy scale fail |
| Spanner BOOL / Avro boolean | `boolean` | strings are not booleans |
| Spanner STRING / Avro string or enum | `text` (or native undeclared text outside strict mode) | strict mode requires the declaration |
| Spanner DATE / Avro logical date | `date` | compare the calendar date |
| Spanner TIMESTAMP / Avro logical timestamp | `timestamptz` or `timestamp`, matching whether an offset is present | compared to the nanosecond; golden fractions have at most nine digits |
| Spanner BYTES / Avro bytes or fixed | `binary:hex` or `binary:base64` | a byte value needs its declared encoding |
| Spanner JSON, compatible arrays / Avro nested records or arrays | `json` | every nested value must be JSON-compatible; arrays of raw bytes or dates need a supported projection. Spanner client JSON decoding can round numbers, merge duplicate keys and conflate JSON null with SQL NULL; project raw text to check those distinctions |
| nullable field / Avro union with null | the non-null value's declaration | `<null>` is null; incompatible mixed scalar branches need a supported projection |

Kafka paths include the record section. For one message with JSON key `{"id":1}`
and decoded Avro value `{"id":1,"active":true}`, the hand-derived golden header is
`key.id,value.id,value.active` and its row is `1,1,true`.

<!-- snippet: fragment -->
```yaml
exact: {version: 1, max_rows: 10, max_bytes: 4096}
assert:
  data:
    - target: ${KAFKA_TGT_TOPIC}
      db: kafka
      project: kafka_record
      keys: [key.id, value.id, value.active]
      match: expected/messages.csv
      exact:
        columns:
          key.id: integer
          value.id: integer
          value.active: boolean
        order: sequence
```

Use a single-partition topic for this sequence claim. Every decoded frame counts
as a row, including duplicates; each frame retains its message key. The top-level
`exact.max_rows` bounds decoded frames (default 100000, maximum 1000000), and
`exact.max_bytes` bounds both Kafka wire bytes and canonical comparison bytes
(default 64 MiB, maximum 512 MiB). There is no separate frame-limit setting. Reads
must consume the complete captured watermark range before their deadline.
This is a worked syntax example, with hermetic coverage; it is not a recorded
live qualification of Kafka exact.

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
| `sink` | **yes** | — | `db` (table), `file` (file) or `kafka` (topic) |
| `readiness` | **yes** | — | `{kind: ..., ...}`: when the app is ready for the test's `post_start` data |
| `completion` | **yes** | — | `{kind: ..., ...}`: when the app has finished |
| `stability` | no | `5s` | how long the witnessed value must then stay unchanged; `500ms`, `5s`, `2m` or a number of seconds |
| `deadlines` | no | readiness `60s`, completion the test's `timeout` | `{readiness: ..., completion: ...}`, each greater than 0 |
| `reset` | no | `owned` | `owned` only: the run removes exactly what it created |
| `sentinel` | when a kind is `sentinel` | — | the sentinel's SQL and where to watch it (below); refused when no kind uses it |

### Kinds

| kind | for | keys | satisfied when |
|---|---|---|---|
| readiness `baseline-landed` | `initial-load` | `source: {db, table}`, and `target: {db, table}` (sink `db`) or `path:` (sink `file`) | the app is RUNNING, or has already finished (below), and the target's row count, or the file's line count, equals the source's count taken before deploy |
| readiness `source-progress` | `cdc` | `db: postgres-source` | the replication slot `${PG_SLOT}` is active |
| readiness `sentinel` | `cdc`, sink `db`, `file` or `kafka` | none | the ready insert and delete were witnessed; append sinks acknowledge deletion with the declared signal |
| completion `source-count` | `initial-load`, sink `db` | `source: {db, table}`, `target: {db, table}` | the target's count equals the source's, and is above 0 |
| completion `row-count` | sink `db` | `db`, `table`, `expect` (above 0) | the table's count equals `expect` |
| completion `file-lines` | sink `file` | `path`, `lines` (above 0) | the file has `lines` lines, counted across its rollover files, blank lines ignored |
| completion `sentinel` | `cdc`, sink `db`, `file` or `kafka` | none | the done sentinel, inserted after the `post_start` data, proves both insert and delete delivery |

Zero is never proof: `expect: 0` and `lines: 0` are refused, and a source baseline of 0 fails the run
with `zero-count`. For a test whose correct result is an empty target, use the `sentinel` kinds
(`samples/live/04-lifecycle-check`).

An initial-load app can load its rows and stop before the framework first reads its status: it
is then `COMPLETED`, or `QUIESCED` on releases that quiesce it (a DatabaseReader with
`QuiesceOnILCompletion: true`, for example). With `mode: initial-load` that status passes the
RUNNING step, because the app has run; the readiness and completion counts still decide, so a
finished app with missing rows fails. The smoke record names the status it saw, such as
`COMPLETED (bounded source finished)`. With `mode: cdc`, an app that is `COMPLETED` or `QUIESCED`
before it was seen RUNNING fails at once with `terminal-status:<status>`. Without `lifecycle:`,
`assert.smoke` waits for RUNNING and fails an app that finishes before RUNNING is seen or
within its one-second settle after it.

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
target, runs `delete`, and waits for absence (database) or the declared deletion signal (append sink). Only a row the app carried from source to target can
satisfy it. The source table must accept a row that has only the key and the values your `insert`
file gives: fill any `NOT NULL` columns there.

### Rules

- **Routes.** Sentinel source SQL and replication-slot progress use Postgres.
  Table-count witnesses use Postgres, or either Spanner dialect when the case
  declares `audit: {version: 1}` with an audited append-only output. Those Spanner
  baseline/source-count/row-count probes require positive owned counts; zero is
  never completion proof. Sentinel output may be Postgres, either Spanner dialect,
  a freshly owned Kafka topic, or an owned file.
  File sinks require Docker. Kafka completion must be sentinel, with sentinel
  or source-progress readiness. See the output references below.
  BigQuery targets additionally support finite `initial-load` counts and bounded batch MERGE CDC sentinels.
  BigQuery CDC requires Postgres source injection and sentinel readiness/completion; row-count completion is refused.
  The framework allocates a fresh ledger-owned per-test dataset before DDL/TQL to contain generated staging.
  Pre-existing datasets are refused and the shared bootstrap dataset is preserved. Use MERGE: APPENDONLY
  cannot satisfy an insert/delete witness. See [the MERGE sample](../samples/live/bigquery-batch-merge/).
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
those, by exact name: Postgres tables and replication slots, BigQuery tables, acquired Spanner sink tables and Kafka topics, the namespace, and `${OWNED_DIR}`. An
object with the same name that the run did not create is left alone and fails the run
(`collision:<kind>:<name>`). Tables on other database engines are not tracked and are reported as unverified.
With `--keep-resources` nothing is removed. Replay can remove the saved Postgres/file
resources; it refuses prior-attempt Spanner/Kafka entries without incarnation proof.
See [the exact-name cleanup guidance](TROUBLESHOOTING.md#prior-attempt-spanner-or-kafka-resources).

### Sentinel output references

The insert/delete SQL executes through the declared Postgres source route. The
application must transport both events to the observed sink, with a stable
identity and deletion signal. Append sinks keep both events in their output;
empty output cannot prove that deletion arrived. These snippets show lifecycle
and bounded absence together; supply the source DDL, SQL files and app route in
the case. The prohibited identity is explicit and distinct from sentinel IDs.

**File.**

<!-- snippet: fragment -->
```yaml
lifecycle:
  version: 1
  mode: cdc
  sink: file
  readiness: {kind: sentinel}
  completion: {kind: sentinel}
  sentinel:
    db: postgres-source
    insert: sentinel_insert.sql
    delete: sentinel_delete.sql
    observe:
      path: ${OWNED_DIR}/events.json
      key: data.id
      deletion: {metadata.OperationName: DELETE}
assert:
  file:
    - path: ${OWNED_DIR}/events.json
      nothing:
        window: 3s
        read_timeout: 5s
        where: {data.id: 999}
```

**Kafka.**

<!-- snippet: fragment -->
```yaml
lifecycle:
  version: 1
  mode: cdc
  sink: kafka
  readiness: {kind: sentinel}
  completion: {kind: sentinel}
  sentinel:
    db: postgres-source
    insert: sentinel_insert.sql
    delete: sentinel_delete.sql
    observe:
      db: kafka
      topic: ${KAFKA_TGT_TOPIC}
      key: value.id
      deletion: {value.operation: DELETE}
      payload: json
assert:
  data:
    - target: ${KAFKA_TGT_TOPIC}
      db: kafka
      payload: json
      nothing:
        window: 3s
        read_timeout: 5s
        where: {value.id: 999}
```

**Spanner.**

<!-- snippet: fragment -->
```yaml
lifecycle:
  version: 1
  mode: cdc
  sink: db
  readiness: {kind: sentinel}
  completion: {kind: sentinel}
  sentinel:
    db: postgres-source
    insert: sentinel_insert.sql
    delete: sentinel_delete.sql
    observe:
      db: spanner-google
      table: ${TID}tgt
      key: id
assert:
  data:
    - target: ${TID}tgt
      db: spanner-google
      nothing:
        window: 3s
        read_timeout: 5s
        where: {id: 999}
```

The Kafka example assumes the formatter emits id and operation fields in its
message value. Its topic must be freshly acquired by this attempt. Native mode
is possible for Kafka with the required services and owned topic; file sentinels
require Docker access to an owned output directory. Spanner uses owned tables;
spanner-postgres selects the other dialect. These routes have hermetic coverage;
sample 10 exercises the file route and sample 18 exercises the Kafka route.

Kafka and file sentinels read the complete append sink on each poll. Their fixed
limits are `DEFAULT_MAX_ROWS = 100000` and `DEFAULT_MAX_BYTES = 67108864` (64 MiB),
including application data and sentinel insert/delete records. Exceeding either
fails readiness/completion with `canonical-limit-exceeded`; raising `exact:` limits
does not change these sentinel limits. Keep the complete append output within
both limits or use a database sentinel. Spanner database sentinels count only the
requested key with a bounded `WHERE` query. Database sentinel keys must be plain
column identifiers; only Kafka/file keys accept dotted field paths. String values
in `deletion` selectors render tokens just like `nothing.where`.

Owned `spanner-postgres` DDL requires unquoted lowercase, unqualified table names.
Mixed-case, quoted or schema-qualified names are refused before executing DDL,
so catalog confirmation and cleanup always refer to the same table name.

## Infrastructure ownership (`SLT_INFRA_OWNERSHIP`)

Not a `test.yaml` key, but it decides what a test can assume. Every live run declares it in `.env`
or the shell, and a run without it is refused.

- **`shared`** (with `SLT_KEEP_SERVICES=1`, as `.env.example` has it): the run reuses the Striim
  cluster and the service containers that are already up, starts what is missing, and never tears
  anything down or redeploys the cluster. Other people's tests may be running on the same Striim and
  databases. So every name carries `${TID}` or `${NS}`, and a test never assumes an empty database,
  a fresh cluster or a quiet server log.
- **`exclusive`**: the run starts a stack of its own, refuses to start when anything already answers
  on its Striim endpoint, and tears the stack down at the end unless `SLT_KEEP_SERVICES=1`. With
  `--parallel` the controller tears down what its workers started.

When an OP deploy fails because the cluster's OP class loader is poisoned (`invalid LOC header`
in the server log since the case started), only an `exclusive` serial run restarts the app nodes
and retries. Any other run fails the case with `[slt-op-poisoned]`; restart the app nodes with
`python -m livetest.preflight --restart-app-nodes` once nothing else runs on the cluster.

`recover: {mode: kill}` kills every app-node container when `node:` is absent, so on a shared cluster it kills the other
tests running there too. Give such a test `disabled_parallel:`, and do not run it on a cluster others
use.

## Unknown keys are rejected

A manifest key the loader does not read is an error naming the key and listing what is allowed.
This catches typos (`timout: 30` never applied its timeout) and keys dropped in a schema change,
both of which used to load silently and take the default — which is how a CDC test ends up
seeding before its reader starts and failing for a reason the manifest does not show.

The same rule applies inside `op:`/`udf:` entries and to `assert:` tier names, in live and
integration cases alike.

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
- Plain string — uploaded as `f"{TID}<name>"` (per-test; `${TID}` is never empty, serial runs included). The test's own TQL must reference that literal `${TID}`-prefixed name.
- `{from, to}` — `from` is the example/test-dir-relative source file; `to` is the uploaded name, token-rendered if it contains any `${...}` (e.g. `${NS}-<name>`), fully author-controlled (no automatic prefix). If the TQL contains a literal `UploadedFiles/<from>` reference (e.g. a shipped example's clean, untokenized `ConfigFile:` line), the runner rewrites it to `UploadedFiles/<rendered to>` before deploy — so a customer-facing example can keep an untokenized `ConfigFile` while the test controls the uploaded name directly. **`to` is not auto-tokenized**: if several tests can run concurrently and upload the same basename (e.g. `products_lookup.json`), give `to` a `${NS}`/`${TID}` prefix to keep them from clobbering each other in the shared `UploadedFiles/`; a bare `to` (matching `from`) is only safe when nothing else uploads that same name at the same time. `to` must render to a plain file name: a `/`, `\`, `.` or `..` is refused. Teardown deletes an uploaded file only when its name carries the case's `${TID}` or `${NS}`; a bare name is left in `UploadedFiles/`.

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

### `placement` — live flow ownership

Use a list of `{flow, node, timeout}` specs as defined under Single-node faults
and flow placement. It is also an accepted `xfail.tiers` name; recovery failures
remain framework failures regardless of that marker.

### `data` — database rows and Kafka records
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
distinct sets; pin `rows: N` too if cardinality matters. A golden row's missing trailing cells
are NULL (prefer `<null>`); a row with more cells than the header is refused.
On mssql and teradata, `rows` and `min_rows` count committed rows only; if the writer still holds
the table lock at `timeout`, a `min_rows` spec passes on the uncommitted count, so it gives no
committed-row guarantee where a writer may roll back or retry; use `rows:` for that.
A Kafka count reads every offset up to the high watermark taken when the read starts (offsets
that hold no message, such as transaction markers, count as read). It keeps polling while the
topic does not exist or offsets are unread, and fails at `timeout`; a missing topic never counts
as 0 rows. A partial read can satisfy `min_rows`, never `rows` or `match`.

Database data specs can add `exact:` as described above. Kafka typed comparison
uses `project: kafka_record` with full `key.*`/`value.*` projection paths. Its
optional `payload` selects `avro` (default) or `json` and requires
`exact.strict_json: true`; neither key creates a new assertion tier.

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
the source's. On a SQL database target each poll counts the target first and reads it in full only
when the count can match (`exact: true`: the source's row count; otherwise at least its distinct
rows); with `exact: true` the source is also only counted until then. The time it reports
(`elapsed_s`) runs from the app's start, or from the last input after it (a `post_start` seed,
generate, transaction step or server file; the end of a `recover:` or `action:` phase), to when
the poll that passed began reading its last target. `elapsed_upper_bound: true` marks a time that
is only an upper bound: the target was already equal at the first poll, or a source read inside
the polls delayed the observation.

For a typed Spanner diff, both endpoints name the table within the selected
service database, and every non-text column is declared. This checks counted
rows rather than the legacy distinct text set:

<!-- snippet: fragment -->
```yaml
exact: {version: 1}
assert:
  diff:
    - source_db: spanner-google
      source: '${TID}src'
      target_db: spanner-google
      target: '${TID}tgt'
      exact:
        columns: {id: integer, amount: 'decimal:2'}
```

For the PostgreSQL dialect, use `spanner-postgres` on each endpoint. A declared
lifecycle requires both read tables to be owned by this attempt. Derive the
source rows by hand from the seed; copying `1,12.30` twice requires two identical
target rows, and a missing copy or changed amount fails. This is a route-binding
example, not a claim of live Spanner diff qualification.

### `file` — `FileWriter`/`JSONFormatter` target assertions

File specs can add typed `exact:` or use a standalone `raw:` inventory as
described above. A raw spec takes only `path` and `raw`: content/projection
keys cannot coexist with the inventory.
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
      sha256: <hex digest>           # one of content_hex / sha256 / size; bare or sha256:<hex>
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
`user_data key=('1',): questions[0].answers[0].RESPONSE (expected 'R1', got None)`. A boolean
equals only a boolean: `true` is not `1`. Polls until every key matches or the manifest `timeout`
expires.

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
`{absent: true}` (missing or null; an empty `MON` body fails instead) and `{matches: <regex>}`. Strings, patterns and component names are
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

Polls `SHOW <app> CHECKPOINT HISTORY;`. `nonempty` passes once a checkpoint is shown. `empty`
reads until `timeout` and fails as soon as one is shown, so set `timeout` longer than the
`RECOVERY` interval it must rule out. Use it to tell an app with a `RECOVERY` clause from one
without.

#### `predates` — prove a `recover:` case replayed a seed

<!-- snippet: fragment -->
```yaml
seed:
  - {file: seed_a.sql, db: mysql-source, when: post_start, after: 5s}
  - {file: seed_b.sql, db: mysql-source, when: post_start, after: 80s}
recover:
  mode: kill
  after: 10
assert:
  checkpoint_history:
    predates: seed_b.sql
```

Passes when the last checkpoint Striim recorded before the interruption is older than the named
seed, so the restart must have replayed that seed's events. A crash case otherwise passes
unchanged when a checkpoint lands between the seed and the kill, with nothing replayed.

The history is read three times: just before the named seed runs, just before the interruption
command, and as soon as the app is RUNNING again (before `post_recover` seeds and the settle). The
case fails when:

- the first read shows no checkpoint (nothing anchors the replay);
- a later read shows a checkpoint the first did not;
- a read fails, or a row has no `checkpointRecordedTime`.

Rows are compared with each other, never with the harness clock, so the server's time zone does
not matter. A checkpoint recorded between the interruption read and the command appears in the
RUNNING read. A checkpoint the restored app records before that read also fails the case: with a
one-minute `RECOVERY` interval that is unlikely, and it is a false failure, never a false pass.

Requirements, refused at load otherwise: `recover:` with `times: 1`, no `node:`,
`expect_running: true` and a mode other than `quiesce` (it records a checkpoint as it stops);
exactly one `seed:` entry with that file and `when: post_start`. The mapping takes no other key.

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
  Docker-only `${STRIIM_PRIMARY_SERVER}`, `${STRIIM_SECONDARY_SERVER}` and
  `${STRIIM_AGENT_SERVER}` resolve to live server names from inspected framework
  containers. They are reserved, cannot be overridden by `tokens:` or environment
  values, and require readable node inventory. Use them in `deployment_groups`
  instead of hard-coded runtime names. Tokens are populated when
  `deployment_groups` is declared or the TQL references a server token; other
  locations alone do not trigger inventory resolution.
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

Worked case: [07-spanner-exact](../samples/live/07-spanner-exact/README.md).

Kafka `payload: json` comparisons retain their file/record declaration rules in evidence validation, including `strict_json: true` and record sequence order without a database `order_by`. Passing and failing comparisons both retain a validated envelope.

Worked case: [09-raw-file](../samples/live/09-raw-file/README.md). Framework gallery: [framework-raw-file](../scripts/live/regression/framework/framework-raw-file/README.md).

Absence selector string values expand case tokens before matching. Finite numeric selectors compare by decimal value, so YAML `9.99` matches a delivered numeric `9.99`; booleans and strings retain distinct token types. Missing fields or incompatible output types still fail the read.

Worked case: [10-bounded-nothing](../samples/live/10-bounded-nothing/README.md). Framework gallery: [framework-bounded-nothing](../scripts/live/regression/framework/framework-bounded-nothing/README.md).

Transaction seed SQL refuses `ABORT` along with other transaction-control statements; use a declared transaction step so session lifetime and rollback remain observable.

Worked case: [11-transaction-seeds](../samples/live/11-transaction-seeds/README.md). Framework gallery: [framework-transaction-seeds](../scripts/live/regression/framework/framework-transaction-seeds/README.md).
Snapshot prefix indices must never decrease: `snapshot-prefix-regressed` fails a history that returns to an earlier committed state, even if it later reaches the final state. Repeated reads of the same prefix are allowed; sampling can skip intermediate prefixes.

Worked case: [12-snapshot-observer](../samples/live/12-snapshot-observer/README.md). Framework gallery: [framework-snapshot-observer](../scripts/live/regression/framework/framework-snapshot-observer/README.md).
Audit writer declarations accept an optional `VERSION` clause. KafkaWriter `Topic` must name the audited topic; the compatibility spelling `Topics` is also accepted, but specifying both is refused as ambiguous.

Worked case: [13-append-only-audit](../samples/live/13-append-only-audit/README.md). Framework gallery: [framework-append-only-audit](../scripts/live/regression/framework/framework-append-only-audit/README.md).
After an interruption takes effect, a fresh progress read has at most five seconds (or the shorter trigger timeout). A target count at or above `below`, or an unreadable count, fails `fault-missed`, even when the pre-command read held. Other trigger kinds retain a failed post-interruption read as diagnostic `error`/`errorTruncated` fields with observation times; they do not fail the fault merely because a whole-cluster kill removed the checkpoint API. Absolute triggers require `times: 1`; only a checkpoint trigger without `above` supports repeated cycles with an advancing baseline.

Worked case: [14-progress-recovery](../samples/live/14-progress-recovery/README.md). Framework gallery: [framework-progress-recovery](../scripts/live/regression/framework/framework-progress-recovery/README.md).

Fault evidence retains `windowOpened` (the first in-window value and time), the pre-command value, `issuedAt`, and the bounded `afterInterrupt` observation. When work finishes during STOP, the record is failed with `interrupted: true`; no command is retried. Timed recovery also requires RUNNING at issuance.

Worked case: [15-fault-evidence](../samples/live/15-fault-evidence/README.md). Framework gallery: [framework-fault-evidence](../scripts/live/regression/framework/framework-fault-evidence/README.md).

Worked case: [16-recovery-sentinel](../samples/live/16-recovery-sentinel/README.md). Framework gallery: [framework-recovery-sentinel](../scripts/live/regression/framework/framework-recovery-sentinel/README.md).
A hosting-node fault resolves all owners before post-start seeds. At issuance it checks flow MON's single-owner count and the selected server's deployment and runtime STATUS once, outside progress polling, then re-reads progress. An unreadable, moved or multi-owner flow refuses the fault. Sample17 paces each target insert with a10ms table CHECK, leaving the exact expected rows unchanged.

Worked case: [17-one-node-failover](../samples/live/17-one-node-failover/README.md). Framework gallery: [framework-one-node-failover](../scripts/live/regression/framework/framework-one-node-failover/README.md).

## Intentional idle Postgres transactions

Disposable Postgres has no data-role idle-transaction budget by default, because Striim
readers and writers use those same roles. If you opt in with
`SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS=600000`, an idle transaction can terminate its session
after ten minutes. That includes a [`transactions:`](#transactions--overlapping-postgres-seed-sessions)
session held across `recover:`. For one, put
`SET LOCAL idle_in_transaction_session_timeout = '30min';` in the session's first `op: sql` file, or set
`SLT_PG_IDLE_TRANSACTION_TIMEOUT_MS=1800000` before framework bring-up. Policy is reapplied
to new connections even on kept services; existing connections retain their settings.
Budget the full idle period including recovery; keep expected rows and lifecycle assertions.
[SERVICES.md](SERVICES.md#postgres-postgres) describes the scope and integration setting.
External databases retain their own policy.
