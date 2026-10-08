# Performance Testing Extension for the Integration Framework

> **Where this lives.** This specification lives with the harness it specifies, in `striim-testing-framework-utility`: `scripts/integration/inttest/perfmanifest.py` (Section 3 loader), `inttest/perf.py` (pre-flight, reset, launch, sampling, correctness), `inttest/perfreport.py` (Sections 11-12), the perf half of `inttest/plugin.py` (Section 14), the Java driver `PerformanceProcessor` with `OperatorCore`/`UdfCore`/`SourceCore`/`TargetPerfCore`, and the hermetic tests `tests/test_perf*.py`. The perf cases live in your test repo, under `scripts/integration/perf/` — which may pin this repo by commit and loads the plugin through a pytest plugin of its own (`-p <launcher>` in its `scripts/integration/pyproject.toml`). The tier's entry points (the runners that drive the `perf` tier and its reports, Section 16) live there too. Code cites this document by section number; section numbers are stable.

## 1. Purpose

The performance-testing extension runs selected integration tests repeatedly using a dedicated, deterministic performance input file. It measures processing under increasing workloads while preserving existing integration-test behavior.

**The subject of a performance test is the operator's `Processor`, or — for a `udf:` case — the UDF's own static functions.** For an OP case this is the same unit the integration tier already exercises: the jar named by `op.jar`, constructed from the `properties:` map, driven with WAEvents, producing WAEvents. For a `udf:` case the subject is the pipeline of `public static` calls named by `udf.pipeline`, driven directly against one input value per event (a `WAEvent` or a parsed `JsonNode`, per `udf.kind`), with no `Processor` and no constructor involved. Two further shapes reuse the same loop: a `source:` case drives a READER by ticks (Section 4, "Readers"), and a `target:` case drives a WRITER whose events are accepted and flushed once per window (Section 4, "Writers"). Nothing inside the driven unit is instrumented: no internal class, cache backend, store implementation, key builder, generator, or helper is measured on its own. Measurement is strictly black-box, at the process and event boundary, in both cases.

A performance test is therefore an integration test with:

- a dedicated performance input file;
- explicit replay counts;
- optional expected-output validation;
- warmup and measured iterations;
- process-level metric collection (wall clock, CPU, resident memory); and
- a human-readable and machine-readable result report.

This framework supersedes the per-op Java `*Perf.java` / `run-perf.sh` / `perf-chart.sh` harnesses. Section 15 states plainly what that replacement does and does not carry over.

## 2. Execution modes

Two mutually exclusive execution modes, over two mutually exclusive collection trees (Section 13):

- `pytest`: integration mode. Collects `scripts/integration/regression/**/test.yaml` exactly as it always has. `scripts/integration/perf/**/test.yaml` files ARE shape-validated at collection time regardless of `--perf` (`pytest_collect_file` dispatches by location before `--perf` selection is known, Section 14) — a malformed perf `test.yaml` is a collection ERROR in either mode, the same way a malformed regression `test.yaml` already is, and can abort the run before any regression test executes. What `perf/` cannot do is change a regression test's OWN behavior or outcome: no regression `test.yaml`/fixture/pytest hook/dev-tool result differs based on what exists under `perf/`, only whether collection itself succeeds.
- `pytest --perf --perf-reverse`: performance mode with the **ordering control** (§16). Runs the selected items in REVERSE order — both a case's `variants:`/`matrix:` runs and the sequence of cases, since one reversal of the selected list covers both. Item names are unchanged, so a reversed run's reports pair against a forward run's by run id. ⚠ **Adjacent `matrix:` permutations have differed by 4-10% on running order alone, so any result under ~10% is indistinguishable from the order it was measured in**; run both directions and keep only the differences whose sign holds. Without `--perf` this is a `pytest.UsageError` rather than a silent no-op.
- `pytest --perf`: performance mode. Collects `scripts/integration/perf/**/test.yaml` only, running the performance semantics in this specification for every one that isn't `disabled:` (Section 3 — the same shared key a regression `test.yaml` uses, not a perf-specific flag); `scripts/integration/regression/**/test.yaml` is not collected as a test in this mode (a perf `test.yaml` is fully self-contained, Section 3, so nothing under `regression/` is ever consulted in this mode).

Performance mode never silently falls back to integration semantics. If the selection is empty after filtering, the run fails with a `pytest.UsageError` and a nonzero exit code naming what was searched.

Skips remain skips, exactly as in the integration tier; among them: a missing `STRIIM_HOME`, missing `java`, no Docker or no compose file for a declared service, a build environment that cannot build the op jar (no JDK for the release, a `STRIIM_HOME` that does not match) or an unbuildable harness jar, and a `disabled:` case all produce a skipped test, not a failure. An op jar whose `mvn package` (or its `mvn clean package` retry) runs and fails, or that builds but leaves no single `*-<series>.jar`, is a FAILURE: the module is broken, and a skip would let a run that tested nothing of it read green. A `docker compose up` that runs and exits nonzero is a FAILURE, not a skip (Docker is present and the service is declared; folding that into a green run hid a broken environment once). Pre-flight failures (Section 3) are failures too.

## 3. Configuration

A performance test is its own `test.yaml`, living in its own directory tree — `scripts/integration/perf/{op,udf}/<name>/<case>/` — parallel to, and never mixed into, `scripts/integration/regression/`. This keeps the fact that most integration test dirs have no performance counterpart from ever showing up as an empty/optional file inside `regression/`: a case only appears under `perf/` at all if it has one.

A perf `test.yaml` is STRUCTURALLY IDENTICAL to a regression `test.yaml` (Section 3's own required/optional fields: `name:` plus exactly one of `op:`/`udf:` required, and optionally `properties:`/`purpose:`/`requires:`/`ddl:`/`seed:`/`timeout:`/`disabled:`/`types:`/`password_properties:`/`source:`/`target:`/`variants:`/`matrix:`) except `assert:` is replaced by `performance:` (this section). Unknown keys are rejected at every level — top level, `performance:`, `jvm:`, `correctness:`, `metrics:` — so a misspelled key (`warmup_run:`) is a manifest error, not a silent fall-back to the default.

**`variants:` and `matrix:` are the two fan-out axes, and they compose.** `variants:` is the CONNECTION axis (engine, URL, provider, credentials, and that engine's own `ddl:` and route); `matrix:` is the PROPERTY axis (a property name to the list of values to cross-product into `properties:`). `TestManifest.runs()` yields every `(variant, permutation)` pair, and performance mode turns EACH ONE into its own pytest item, with its own console block and its own JSON report — unlike the regression tier, which loops both axes inside a single item against a single `assert:`. That difference is the point: a perf run's product is a NUMBER, and the difference between two runs one property apart is the deliverable, so they must not be averaged or picked between. The same refusals as the regression loader apply — a case with `variants:` may not carry a case-level `ddl:` or `seed:` (each engine needs its own SQL through its own route), and neither axis has any meaning for a `udf:` case.

⚠ **`matrix:` therefore means the OPPOSITE of what it means in the regression tier.** There, N permutations share one `assert:` and the claim is that the knobs are NOT observable in the output. Here each permutation is its own measurement and the claim is that they ARE.

`op.jar` names the module reference for an OpenProcessor case; `udf.jar` names it for a UDF case (`docs/INTEGRATION-TESTS.md` has the full `udf:` schema) — a `udf:` block is fully self-sufficient, with no `op:` block at all, and drives the module as a bare UDF pipeline instead of as a `Processor`, rejecting `properties:`/`password_properties:` outright since neither has any meaning for a UDF. There is no pointer to a regression case and no config reuse — a perf `test.yaml` states its own `op:`/`udf:`/`properties:` directly, even when a regression case for the same operator happens to use the same values, since there are only ever a couple of perf `test.yaml`s per operator and duplicating a few `properties:` lines is not a real cost. Authoring a perf test.yaml is: copy an existing regression `test.yaml`, delete `assert:`, add `performance:`.

Every relative reference (`ddl:`/`seed:` files, a `ConfigFile` property, `${TEST_DIR}`, Section 9) resolves against the perf `test.yaml`'s own directory — there is only one directory involved, unlike the regression tier's own case where `assert.data[].input`/`match` are the only things resolved elsewhere. `performance.input`/`performance.expected` (and any other performance-only fixture) also resolve against that same directory.

Example: a regression case, `scripts/integration/regression/op/myop/myop-uppercase-names/test.yaml`, untouched:

```yaml
name: myop-uppercase-names
purpose: >
  uppercase mode uppercases every string column;
  a row that is already uppercase is unchanged

op:
  jar: java/OpenProcessors/MyOp

properties:
  Mode: 'uppercase'
  EnableLogging: 'false'

assert:
  data:
    - input: input/customers.json
      match: expected/customers.json
```

and its performance counterpart, `scripts/integration/perf/op/myop/myop-uppercase-names/test.yaml` — the same `name:`/`op:`/`properties:`, `assert:` replaced by `performance:`:

```yaml
name: myop-uppercase-names

op:
  jar: java/OpenProcessors/MyOp

properties:
  Mode: 'uppercase'
  EnableLogging: 'false'

performance:
  input: performance/customers.json
  expected: performance/expected-customers.json

  run_sizes: [30k]
  warmup_runs: 100
  measured_runs: 3
  timeout: 120

  jvm:
    min_heap: 512m
    max_heap: 2g
    args: []

  correctness:
    mode: sampled # full | sampled | disabled

  metrics:
    capture_cpu: true
    capture_memory: true
```

with `performance/customers.json` and `performance/expected-customers.json` living alongside that second `test.yaml`, under `scripts/integration/perf/op/myop/myop-uppercase-names/performance/` — not under the regression case at all (Section 13).

A UDF case's perf `test.yaml` is the same shape, with a self-sufficient `udf:` block (its own `jar:`, no `op:` block at all) in place of `op:` — `scripts/integration/perf/udf/referenceudf/referenceudf-mark-processed/test.yaml`:

```yaml
name: referenceudf-mark-processed

udf:
  jar: java/UserDefinedFunctions/ReferenceUdf
  class: com.example.ReferenceUdf
  kind: waevent
  pipeline:
    - function: ReferenceUdfSetLogging
      args: [false]
      as: logging_off
    - function: ReferenceUdfMarkProcessed
      args: ['$']

performance:
  input: performance/customers.json
  expected: performance/expected-customers.json
  run_sizes: [30k]
  warmup_runs: 100
```

(The committed file also spells out `measured_runs`/`timeout`/`jvm`/`correctness`/`metrics`; the shape is the point here.)

A case with no directory under `perf/` is not a performance test; there is nothing to disable, because there is nothing to have written in the first place. A perf `test.yaml` that exists is always shape-validated (`name:`, exactly one of `op:`/`udf:`, and the `performance:` block must satisfy every rule in this section) — a malformed perf `test.yaml` still fails loudly at collection rather than being silently ignored, matching how the regression tier already treats a malformed `test.yaml`; the shared `disabled:` key (Section 3's own list, below) does not change this — even a disabled perf test.yaml is fully shape-validated, exactly as a `disabled:` regression `test.yaml` already is.

### Required fields

- `name` plus exactly one of `op`/`udf`: the same fields the integration tier's own manifest loader requires unconditionally for any `test.yaml`. (`properties:` is OPTIONAL — an operator that takes no configuration of its own has nothing to put there; absent and an explicit empty mapping both normalize to `{}`. A `udf:` case rejects `properties:` outright rather than defaulting it away, since a bare UDF pipeline has no constructor to configure at all.)
- `performance.input`: relative to the perf `test.yaml`'s own directory, path to the dedicated performance input file (WAEvent-JSON array, same format as `assert.data[].input`). Required for every case EXCEPT a `source:` case, where it is rejected (a reader replays ticks, Section 4). A `target:` case still uses `performance.input`, not `target.input`, as its fixture.
- `performance.run_sizes`: one or more positive size values. Each value is a complete-file replay count (Section 4).

### Optional fields

| Field | Default | Meaning |
| --- | --- | --- |
| `performance.expected` | none | Expected output of ONE replay (Section 6). |
| `performance.warmup_runs` | `1` | Untimed complete replays executed inside each measured iteration's process, before its measured window. `0` is allowed. |
| `performance.measured_runs` | `3` | Independent measured iterations per run size. Minimum `1`. |
| `performance.timeout` | `900` | Per-measured-iteration wall-clock budget, in seconds, covering the whole operator subprocess (startup, warmup, measured window, teardown). Must be positive. Distinct from the top-level `timeout:` key, which is accepted for schema parity but read by nothing in performance mode. |
| `performance.jvm` | none | JVM settings for the operator subprocess. |
| `performance.correctness.mode` | `sampled` | `full`, `sampled`, or `disabled`. |
| `performance.metrics.capture_cpu` | `true` | Enable CPU sampling. |
| `performance.metrics.capture_memory` | `true` | Enable resident-memory sampling. |
| `performance.gcs_prune_prefix` | none | Object-key prefix (rendered, e.g. `'${TID}gcsbw-perf-multi/'`) whose objects are deleted from the test bucket at every iteration's reset (Section 9). Must be non-empty; needs `gcs` in `requires:`. |

A perf `test.yaml`'s `disabled:` block is still shape-validated when present, so a typo in it is caught rather than silently ignored — same as the regression tier already does. (There is no perf-mode-specific `performance.enabled` flag: an earlier revision had one, gating `--perf` selection with a silent deselect-with-no-reason on `false`; removed once it became clear the shared `disabled:` key already covers "not ready to run yet" strictly better — it still selects the test and skips it with a stated reason, visible in the run's output, rather than vanishing into the deselected count.)

### Run-size notation

Run sizes accept an integer, or an integer with a case-insensitive decimal suffix: `k = 1,000`, `m = 1,000,000`, `g = 1,000,000,000`. Fractions (`1.5k`) are not supported. Values expand to exact positive integers before execution; both the authored form and the expanded integer are reported.

Duplicate run sizes (after expansion, so `1000` and `1k` collide) are a configuration error. Run sizes execute in ascending order regardless of authored order, so a cheap size fails before an expensive one is attempted.

Total work grows as `events_in_file × run_size`; the author is responsible for choosing sizes whose product completes within `performance.timeout`. The performance input file is committed to the repository and should stay small (hundreds to a few thousand records); scale comes from `run_sizes`, not from a large fixture.

### JVM configuration

```yaml
jvm:
  min_heap: 512m
  max_heap: 2g
  args:
    - '-XX:+UseG1GC'
```

`min_heap` maps to `-Xms`, `max_heap` maps to `-Xmx`, and `args` are passed through in order, before the classpath, on the operator subprocess command line. An absent `jvm:` block passes no JVM options at all, so it means "JVM defaults" (notably a default max heap of roughly one quarter of physical RAM) — which is machine-dependent and therefore always recorded in the report (`JVM: (defaults)` on the console, an empty `effective_args` in the JSON). The `min_heap <= max_heap` comparison uses the JVM's binary suffixes (`k`=1024), unlike run-size notation below, so `1024m` and `1g` compare equal.

Validation, before execution:

- `min_heap`/`max_heap` must match `^\d+[kKmMgG]?$`, and `min_heap` must not exceed `max_heap`;
- `args` must be a list of non-empty strings;
- `args` must not contain `-Xms`, `-Xmx`, `-cp`, or `-classpath` (they would conflict with the fields above and with the harness's own classpath);
- duplicate entries in `args` are rejected.

Different JVM settings define different benchmark configurations; results are only comparable across runs with an identical effective JVM configuration, which the report records verbatim.

### Validation timing

Two distinct validation stages, matching how the framework already separates them:

1. **Shape validation, at collection time**, in the manifest loader: types, required keys, run-size notation, JVM value formats, mode enumerations, and the `expected`-versus-`correctness.mode` rule (Section 6). A failure here is a manifest error naming the file and the offending key, and it fails the test at collection, before any provisioning, build, or run.
2. **Pre-flight validation, once per pytest item (one per `variants:`/`matrix:` run), before the first warmup replay**: file existence and readability of `input` (and of `expected`, unless `correctness.mode` is `disabled`), non-empty parse of each, and that `psutil` imports. Both fixtures are `${...}`-token-rendered before parsing, the same way the regression tier renders `assert.data[].input`/`match`, so a fixture may reference `${TID}`/`${POSTGRES_SOURCE_SCHEMA}`. The `sampled` index set (Section 6) is computed here from `len(expected)`. The manifest loader deliberately does not touch the filesystem; that policy is preserved.

Both stages complete before any measured work begins. No performance run starts against a configuration that has not fully validated.

## 4. Input and replay semantics

The performance input is separate from the integration-test fixture, so performance data can be shaped for throughput without changing integration coverage.

Let:

```text
events_in_file = number of records in performance.input
```

For each configured `run_size`, one measured iteration replays the complete input file exactly `run_size` times:

```text
measured_input_events = events_in_file × run_size
```

Each replay processes every record exactly once, in file order, with no partial replay, using the same operator configuration (`properties:`, `types:`, `password_properties:`, working directory) as the integration test — or, for a `udf:` case, the same `udf:` pipeline, with no `properties:`/`password_properties:` involved at all.

### Replay mechanism (decided)

Replay is **in-memory, from a pre-parsed template list**. It is NOT a physically expanded file on disk.

- The input file is read and parsed **once**, before the measured window, into an ordered list of `events_in_file` record templates.
- During the measured window, the driver materializes a **fresh event instance per record from its template** for every replay (`WAEvent.makeCopy` or `AvroEvent.makeCopy`, dispatched on the template's kind; any other kind is an error rather than a silent reuse). Operators may mutate the event they are handed; template materialization guarantees replay *n* sees the same input replay 1 saw.
- Per-record materialization from an already-parsed template happens **inside** the measured window. This is deliberate: production also allocates a fresh event per record, so this cost belongs to the measurement. JSON parsing, file I/O, and validation happen **outside** it.

Rejected alternative, for the record: writing an expanded file containing `events_in_file × run_size` records. It costs disk proportional to total work (hundreds of GB at the upper run sizes), the driver reads its input file whole rather than streaming it, and file reading is not the operator's production path — in Striim, readers feed events to a `Processor` in memory. Physical replay would therefore measure the harness's file reader, not the `Processor`.

### Output handling

Emitted output is **not accumulated across replays**. Retaining `events_in_file × run_size` output records is what would make large run sizes impossible, and it would measure allocation of the harness's own result buffer. The driver instead:

- counts emitted records per replay and in total;
- retains records only from the **compared replay** (Section 6) — all of them in `full` mode, only the sampled indices in `sampled` mode, none in `disabled` mode.

### Driver contract

Performance mode uses the same operator-subprocess launch path as integration mode (harness jar + op jar on the classpath, request file argument, test directory as working directory), with a performance request (`PerfRequest`) that carries the op jar, `properties:` (token-rendered, with `ConfigFile` pointing at a per-iteration rendered copy when its contents carry tokens), `types:`, `password_properties:`, the input file, the result file path, the replay count, the warmup replay count, the correctness mode, the sample indices to retain, and — for a `udf:` case — the `udf:` block itself, or — for a `source:` case — the `source:` block, or — for a `target:` case — the `target:` block. The input file the driver reads is a freshly written copy of pre-flight's parsed, token-rendered events, not `performance.input` itself. A `udf:` case is driven by `UdfCore`, a `source:` case by `SourceCore`, and a `target:` case by `TargetPerfCore`, rather than the OP path's `OperatorCore`; the input/output file format, the replay mechanism (a fresh event materialized per record, per replay), the output-handling rules above, and every field the result file must report are otherwise identical across the four — for a UDF, one input event still yields exactly one output event (zero when a `waevent`-kind pipeline's `$` register ends `null`). `matrix:` permutation values are NOT token-rendered (the loader rejects one containing `${`), matching the regression tier, which applies overrides after rendering.

The driver's result file must report at least:

- `measuredStartEpochMillis` and `measuredEndEpochMillis` (for metric attribution, Section 7);
- `measuredDurationNanos` (from a monotonic clock; authoritative for duration);
- `warmupInputEvents`, `warmupOutputEvents`;
- `measuredInputEvents`, `measuredOutputEvents`;
- `comparedReplayOutputEvents`;
- `perReplayOutputEventsStable` (Section 6), and `firstUnstableReplayIndex` when it is false;
- `comparedRecords`: the retained sampled or full records of the compared replay; and
- `errorMessage`, with `errorReplayIndex` and `errorRecordIndex` at which it occurred (the replay index is negative during warmup — warmup replay *w* is `-(w+1)` — so a measured index 0 is distinguishable from a warmup failure).

The result file is written even after an operator error, then the process exits nonzero. A writer's flush failing is such an error: a flush belongs to the window, not to a record, so it is attributed to the window's last replay (the last warmup replay is `-warmupRuns` under the rule above; the last measured replay is `runSize - 1`) with no record index. With no warmup replays there is no warmup flush. A result missing `perReplayOutputEventsStable` fails the iteration rather than defaulting to "stable".

### Readers (`source:`)

A reader has no input at all — it pulls from its source and pushes into a channel — so `performance.input` does not apply and is **rejected** for a `source:` case. Its replay unit is a tick count instead:

```text
measured_input_events = source.max_ticks × run_size
```

One replay is `source.max_ticks` calls to the operator's `tick(<channel>)`. Everything else in this section holds unchanged: the driver is `SourceCore` rather than `OperatorCore`, and warmup, the measured window, per-replay counting, compared-replay retention, and the mandatory checks of Section 6 are the same code on the same contract. "Input events" are ticks — the drive steps — which is exactly what makes that reuse sound.

Two consequences worth stating outright:

- **The source must be replay-stable** (Section 6's emission-count check). The operator is constructed **once**, before warmup, and is *not* rebuilt per replay (Section 5), so a finite source drains during warmup or the compared replay and every later replay emits less. The check catches that; it is not a silent wrong answer.
- **`source.expect_events` is rejected in a perf case.** It stops ticking early once a count is reached, which would make replays different lengths and both `measured_input_events` and the stability check meaningless. A perf replay is always exactly `max_ticks` ticks.
- **The tick trigger is NOT re-materialized per tick.** §4's per-record materialization exists because production allocates a fresh event per record; a reader is handed no input event in production, so copying one per tick would time harness-only work inside the measured window. `SourceCore` ignores the argument, so a single instance is reused.

⚠ **Read a reader's throughput from `output_throughput`, not `input_throughput`.** For a reader `input_throughput` is **ticks per second**, so a reader emitting 1000 events per tick and one emitting 1 report the same figure. Section 8's aggregate is computed from input throughput, which for a reader therefore describes its *poll rate*, not its event rate. `output_throughput` and `measured_output_events` are reported per iteration and are the meaningful numbers. The console block (Section 11) labels a reader's median `ticks/sec` and adds an `Emitted: N events/sec` line (the run size's output events over its median duration); the JSON report marks the case with `input.replay_unit: "tick"`. Making §8 aggregate output throughput for reader cases is a known, deliberate omission — it changes a contract shared with every in-stream op and belongs in its own change.

### Writers (`target:`)

A `target:` case drives a WRITER: each input event is handed to the writer's `accept` with a position attached (one ordinal per event, as the integration tier does, so the writer takes its recovery path rather than the no-recovery one), and the writer emits nothing — `measured_output_events` is always zero for a target case, which is what a writer is, not a defect. What a window costs is its **flush**: `accept` only accumulates, so the driver calls `endOfWindow()` (the writer's `flush`) once per window, **inside the timed region**, so the writer's own `BatchPolicy` governs how often it commits. The warmup's own window is closed, untimed, before the measured window opens, so warmup events are never flushed inside the measurement. `endOfWindow()` is a no-op for every other driver. The fixture is `performance.input`. Of the `target:` block's keys only `distribution_id` changes what a perf case measures: the driver always attaches a position and never restarts, so `restart_after`, `mid_run` and `positions: false` are refused at load with a message pointing at the regression tier, and `target.input` is optional (it is never read; absent, it defaults to `performance.input`). `target.timezone` is **not applied** by the perf launcher: the regression harness runs the driver JVM at `-Duser.timezone=<tz>` (default `UTC`) and the perf launcher passes only the `jvm:` options, so a perf target case runs at the host JVM's default zone. A target case with `correctness.mode: disabled` has no emitted stream to compare, so an incorrect column order or table can still produce a throughput result. This repository does not include a validator that checks generated cases' `types:`, `Tables:` and `BatchPolicy` against their fixtures. Verify those settings and the resulting database rows with an integration case before using the performance result.

### Input requirements

The performance input file must exist, be readable, contain at least one record, and conform to the operator's expected input schema. Malformed records, unparseable JSON, and unsupported formats are configuration/setup failures reported before measurement — never performance results.

## 5. Warmup and measurement

### The protocol, exactly

For each `run_size`, in ascending order, and for each measured iteration `i` in `1..measured_runs`:

1. **Reset the environment** (Section 9). Nothing from a prior iteration survives.
2. **Launch a new operator subprocess.** A fresh JVM, with the configured `jvm:` settings, the built op jar, and the test directory as working directory.
3. **Construct and start the `Processor`** inside that process — or, for a `udf:` case, load the UDF class and resolve its pipeline's static methods (no constructor involved); for a `source:` case build the reader core; for a `target:` case build the writer core. Untimed in every case.
4. **Execute `warmup_runs` complete replays.** Untimed, inside this same process, output counted but discarded. Then close the warmup window (`endOfWindow()`, a flush for a `target:` case, a no-op otherwise), still untimed.
5. **Open the measured window**, execute `run_size` complete replays, call `endOfWindow()` (inside the window), **close the measured window**.
6. **Close the `Processor`**, write the result file, exit the process. Untimed.
7. **Validate and record**, in the Python runner: counts, correctness (Section 6), metrics (Section 7). Untimed.

This yields exactly `len(run_sizes) × measured_runs` operator processes per pytest item — that is, per `(variant, permutation)` run of a case (Section 3).

### What is and is not recreated

- **Between measured iterations (and therefore between run sizes): everything.** The operator process is destroyed and a new one launched; the environment is reset per Section 9.
- **Between the warmup replays and the measured window: nothing outside the driver.** The process, the JVM, the `Processor` instance, and all operator state are deliberately carried into the measured window — that is the entire purpose of warmup. Only the driver's own counters and record buffers reset at the window boundary.
- **Between replays within a run: nothing.** Replay *n+1* starts against the state replay *n* left behind, from fresh input records.

Restarting the process between warmup and measurement would discard JIT compilation, JVM heap shaping, and operator caches — i.e. it would discard the warmup — so it is explicitly not done.

A warmup run is **one complete replay of the input file**, not `run_size` replays.

### Consequences of warmup for stateful operators

Warmup mutates operator state on purpose. For an operator whose output depends on accumulated state (a cache that is cold on first pass and warm afterwards), the measured window observes **steady-state** behavior when `warmup_runs >= 1`, and **cold-start-then-warm** behavior when `warmup_runs: 0`. Both are legitimate benchmarks; they are different benchmarks. `performance.expected` must be authored to match the configured `warmup_runs` (Section 6), and the report always states the value in effect.

### Measured runs

`measured_runs` is the number of independent repetitions used to estimate run-to-run variation. It is not a workload multiplier: every measured iteration processes exactly `run_size` complete replays. A single measured run is allowed but is more exposed to scheduling noise, JIT timing, filesystem effects, and contention. Every configured iteration is measured, reported individually, and folded into the aggregates; none is silently discarded as an outlier.

### The measured interval

The measured interval opens immediately before the first record of the first measured replay is handed to `processEvent`, and closes immediately after the last `processEvent` call of the last measured replay returns and the window-ending `endOfWindow()` (the flush, for a `target:` case; a no-op otherwise) completes. It excludes JVM startup, classloading, operator construction, `start()`, warmup, `close()`, input parsing, result serialization (retained compared-replay records are held as raw events during the window and serialized after it closes), correctness validation, and environment reset. An operator exception inside the window ends it early; the result still records how far it got.

Duration is taken from a monotonic clock inside the operator process; the Python-side subprocess wall time is recorded separately as process overhead but is never used as the measured duration.

## 6. Correctness validation

Correctness is evaluated outside the measured interval, in the Python runner, after the process exits.

### What `performance.expected` means

`performance.expected` is the expected output of **one complete replay**, as produced inside the measured window under the configured `warmup_runs`. It is compared against the **compared replay**: the FIRST replay of the measured window (replay index 0).

This is the only definition that works for both stateless and stateful operators: an operator whose output stabilizes after warmup produces identical output for every measured replay, and one that does not stabilize cannot have a fixed expected file at all.

### Mandatory checks (every mode, every measured iteration)

- No operator error, uncaught exception, or nonzero subprocess exit (a nonzero exit fails the iteration even when the result file parsed cleanly; the failure reason carries the first 15 lines of the subprocess's stderr when a result was parsed, the last 2,000 characters of stderr when none could be — missing or unparseable — and "timed out after Ns", with no stderr, on a timeout).
- Valid, parseable result output of the expected shape (a missing or mistyped field is a failed iteration, not a crash).
- `measuredInputEvents == events_in_file × run_size` (for a reader, `source.max_ticks × run_size`).
- **Emission-count stability**: every measured replay emitted the same number of records. The total is therefore `comparedReplayOutputEvents × run_size`, and a mismatch fails the iteration naming the first deviating replay index. An operator whose emitted-record count varies across identical replays is out of scope for this framework — its throughput is not comparable across run sizes and no fixed expected file can describe it.

  **This does not exclude readers, it defines their contract.** A reader (`source:`, Section 4) is the obvious operator whose emitted count might vary — it drains its source and then goes quiet. What that means is not "readers are unmeasurable" but "a reader's perf case must supply a **replay-stable** source": one that yields the same number of events per `max_ticks` ticks, indefinitely. That is a property of the source seam, which the operator owns (`IntegrationSeams`), and it is the right thing to benchmark anyway — a throughput number should measure the reader's own conversion and assembly work, not an emulator's network. A source that drains is caught by this very check, naming the replay where it ran dry, rather than producing a meaningless number.

### `full`

Compare all `comparedReplayOutputEvents` records of the compared replay against `performance.expected`, using the existing WAEvent semantic comparison (order-significant events; `metadata`/`userdata` compared as maps; `data`/`before` compared as `values[]` plus `present[]`; first differing path reported). A length mismatch fails immediately. Any mismatch fails the iteration.

### `sampled`

Compare a deterministic subset of the compared replay's records against the same indices of `performance.expected`, using the same semantic comparison per record. Both sides must have the same length first: `comparedReplayOutputEvents == len(expected)`, else the iteration fails immediately.

Let `N` be that common length. The sample index set `S` is:

```text
if N <= 1000:
    S = {0, 1, ..., N-1}                       # everything
else:
    head   = {0, 1, ..., 99}                   # first 100
    tail   = {N-100, ..., N-1}                 # last 100
    M      = min(800, N - 200)                 # middle budget
    middle = { 100 + floor(j * (N - 200) / M)  for j in 0..M-1 }
    S      = head ∪ middle ∪ tail
```

`M >= 1` whenever `N > 200`, and `floor(j * (N-200) / M)` is strictly increasing in `j` because `M <= N - 200`, so `middle` contains exactly `M` distinct indices inside `[100, N-100)`. `|S|` is therefore exactly `min(N, 200 + M) <= 1000`.

The set is computed once per pytest item, in pre-flight, from `N = len(expected)` and reused for every measured iteration, so all iterations of all run sizes check identical positions; the driver is handed it as an input and never reads `expected` itself. The driver must return exactly `|S|` retained records, in index order — a different count fails the iteration rather than comparing a shorter prefix. Mismatches are reported at the original absolute index, never the sample ordinal.

### `disabled`

Skip record comparison entirely. The mandatory checks above still apply. `performance.expected` may be omitted.

### Expected-file requirement

If `correctness.mode` is `full` or `sampled`, `performance.expected` is required (validated at collection time). A configuration with no expected file must state `mode: disabled` explicitly; the absence of an expected file never silently downgrades the mode.

### Failure handling

Metrics collected before a correctness failure are retained and reported; the iteration is marked `failed`. Any failed iteration makes the pytest command exit nonzero.

## 7. Metrics

All metrics are process-level. There is no per-class, per-method, per-record, or per-collaborator instrumentation — the measurement boundary is the operator process and the event stream crossing it.

Collected per measured iteration, where enabled:

- total measured duration (seconds, from the driver's monotonic clock);
- measured input events;
- measured output events;
- input throughput;
- output throughput;
- average CPU;
- peak CPU;
- peak resident memory (RSS).

Definitions:

```text
input_throughput  = measured_input_events  / measured_duration_seconds
output_throughput = measured_output_events / measured_duration_seconds
```

Input throughput is the primary metric; output throughput is reported because an operator may filter or multiply records.

### Sampling

CPU and memory are sampled from the Python runner at a fixed 100 ms interval, covering the operator process **and all its descendants** (values summed across the tree), for the whole subprocess lifetime. One background thread collects both families in one pass; it runs at all only when at least one of `capture_cpu`/`capture_memory` is enabled, and disabling one toggle nulls only that family's fields (`sample_count`/`insufficient_samples`/`cpu_count` are still reported from the samples collected). The per-run-size summary reports a switched-off family as `null`. With both disabled no sampler runs and every metric field is `null`.

Samples are attributed to the measured interval by timestamp: only samples whose collection window falls entirely between `measuredStartEpochMillis` and `measuredEndEpochMillis` contribute to the reported averages and peaks. The first sample after process start is a priming sample and is always discarded (a CPU-percent reading needs a preceding reading to be meaningful).

If fewer than 3 samples fall inside the measured interval — a measured window shorter than roughly 300 ms — the CPU and memory figures for that iteration are reported as `insufficient_samples` with the sample count, rather than as a number that would be noise. Duration and throughput are still valid and still reported.

### CPU normalization

**CPU percent is normalized to a single core: 100% means one core fully busy.** A multi-threaded operator on a multi-core machine legitimately reports above 100% — a process saturating four cores reports approximately 400%. This is the standard process-level convention and it is what makes the number comparable across machines with different core counts.

Every report therefore carries, for each iteration:

- `cpu_percent_avg` and `cpu_percent_peak` — percent of one core, may exceed 100;
- `cpu_percent_avg_normalized` and `cpu_percent_peak_normalized` — the above divided by the host CPU count, so 100% means "all cores saturated";
- `cpu_count` — the host CPU count both figures were derived from.

### Memory

`memory_rss_peak_bytes` is the maximum, over in-window samples, of the summed RSS of the process tree. RSS is the operating system's resident-set figure for the JVM process: it includes heap, metaspace, thread stacks, code cache, and native allocations, and it is not JVM heap usage. Interval sampling can miss a spike shorter than the sampling period; the report states the interval so the figure is read with that in mind.

### Not included

Per-record latency, percentile latency (p95/p99), GC statistics, JVM heap accounting, and any measurement of components inside the `Processor` are not part of this specification. Duration is reported as end-to-end processing duration for the measured window.

## 8. Result aggregation

Every measured iteration is reported individually. For each `run_size`, the report also includes, over its successful iterations, for both duration and input throughput:

- minimum;
- maximum;
- mean;
- median; and
- standard deviation (sample standard deviation, `ddof=1`; reported as `null` when fewer than two successful iterations exist).

Median is the standard definition: the middle value for an odd count, the arithmetic mean of the two middle values for an even count. **Median input throughput is the primary comparison value**, being less sensitive to outliers than the mean.

Failed iterations are excluded from the aggregates but remain in the report with status `failed`, their failure reason, and whatever metrics were collected before failure.

The per-run-size CPU and memory figures on the console and under the run size's `cpu`/`memory_rss_peak_bytes` keys are not Section 8 aggregates: they are the mean of the per-iteration CPU averages, the maximum of the per-iteration CPU peaks, and the maximum of the per-iteration RSS peaks, over successful iterations whose samples were sufficient (Section 7); `null` when none were.

There is no cross-run-size trend summary. With cases at a 2-5x size span one would report the amortisation of fixed startup cost over a longer measured
window, not how the operator scales — most cases would come back `mixed`, appearing to speed up with size. A trend is only meaningful when every point is long
enough to be startup-free and the span is wide (10x+); express that as explicit run sizes
on the specific cases where scaling is in question, and read the medians directly.

## 9. Isolation and cleanup

Before every measured iteration, the runner resets:

- the operator subprocess (a new process is launched; none is reused);
- the per-iteration temporary directory holding the request, the rendered input copy, the result, and a rendered `ConfigFile` copy when one is needed — a fresh subdirectory of the item's scratch directory, removed once the iteration's result has been read;
- per-test database isolation, when the test declares `requires:` — see below.

### Database reset

Test DDL in a test repo is not idempotent (plain `CREATE TABLE`), so "reset" cannot mean "re-run `ddl:` in place". The reset is the full per-test isolation cycle the integration tier already performs once per test, performed instead once per measured iteration:

- **Postgres**: `ensure_setup()` the fixed `qasource`/`qatarget` schemas (idempotent), then — in a serial run — `reset_schemas()`, dropping every table in both `CASCADE` and recreating the schemas, or — in a parallel run (`${TID}` non-empty) — `reset_test_objects(TID)`, dropping only this test's `${TID}`-prefixed objects; then run `ddl:` followed by `seed:`. `POSTGRES_SOURCE_SCHEMA`/`POSTGRES_TARGET_SCHEMA` are NOT overridden per test or per iteration; they stay at their fixed `qasource`/`qatarget` defaults (see [Integration engine](INTEGRATION-ENGINE.md#token-isolation)). Per-iteration isolation, where it matters, is by `${TID}` table-name prefix, not a per-test/per-iteration schema.
- **Oracle**: drop every source/target table under the per-test prefix — only when `${TID_ORACLE}` is non-empty, i.e. in a parallel run; a serial run has no prefix and drops nothing before `ddl:` — then run `ddl:` followed by `seed:`.
- **Spanner**: ensure the emulator databases exist (`isolation: none`, no per-iteration drop). **GCS**: ensure the bucket exists (same), then — when the case declares `performance.gcs_prune_prefix` — delete every object under the rendered prefix, so each iteration writes into an empty folder. The bucket is shared, so the prefix is declared rather than derived (`${TID}` is empty in a serial run); a case declares its writer's `FolderName` plus `/`. **SQL Server**: no reset branch in either tier; a case relies on its own `DROP TABLE IF EXISTS` DDL.

The reset is outside the measured interval. Its wall-clock cost (reset plus the Section 10 liveness probe) is recorded per iteration as `reset_duration_seconds`, so an expensive seed is visible rather than mysterious.

A test with no `requires:` and no `ddl:`/`seed:` performs no database work at all.

After the item's last iteration, a final teardown drops the test's Postgres objects (whole schemas in a serial run, `${TID}` objects in a parallel one), Oracle prefix tables, and Spanner `${TID}` tables — skipped when any iteration failed (kept for debugging) or `--slt-keep-resources`/`INT_KEEP_RESOURCES` is set.

### Sequencing

Run sizes and measured iterations execute strictly sequentially. Parallel execution is not supported: concurrent tests contend for CPU, memory bandwidth, and database connections, which destroys comparability. Performance mode rejects pytest-xdist outright (Section 14), including when the integration tier's parallel opt-in is set.

## 10. Failure behavior

- Configuration and pre-flight validation failures (Section 3) stop execution before any performance run begins.
- A runtime error, count-invariant violation, or correctness mismatch marks the **current iteration** `failed` and does not by itself stop the test.
- After a failed iteration, the runner attempts environment recreation (Section 9) and continues with the next iteration or run size, provided recreation succeeds.
- If recreation does not succeed, the remaining iterations and run sizes are marked `not_run` and the test stops.
- The pytest command exits nonzero if any iteration failed, if any run size is `not_run`, or if no performance tests were selected.
- Partial results are always written and reported.

### When is the environment "recreatable"?

Precisely: the runner performs the full reset sequence, then a liveness probe (each declared service accepts a connection — for Postgres, that the fixed `qasource`/`qatarget` schemas are reachable; the temporary directory is writable). The probe deliberately does NOT check emptiness: the reset sequence itself runs `ddl:`/`seed:` before the probe, so the schemas are expected to already hold this iteration's tables by probe time — an emptiness check there would fail every successful iteration, not just broken ones. If the reset or the probe raises, the runner waits 5 seconds and retries **once**. Success on either attempt means recreatable; failure on both means not.

Failure classes that are treated as **recoverable** — the iteration fails, the run continues:

- an operator exception or nonzero subprocess exit;
- a `performance.timeout` expiry whose process tree is successfully killed and reaped;
- a correctness mismatch or count-invariant violation;
- a transient database error during reset that clears on the retry.

Failure classes that make the environment **unrecreatable** — remaining work is `not_run`. The runner distinguishes exactly two, named in the report's `failure_class`:

- `surviving-process`: any descendant of the operator subprocess is still alive after termination, grace period, and kill (an orphan would contend for CPU and corrupt every later measurement). The in-flight iteration is still recorded, as `failed`.
- `reset-or-probe-failed`: the reset or the liveness probe raised on both attempts. This is the one class that covers schema drop or create failing, a declared service unreachable (container exited, connection refused), and an `OSError` from the probe's own write to the scratch directory (notably `ENOSPC`); the underlying error says which. An `OSError` while creating the iteration directory or writing `input.json`/`request.json` is NOT covered: it escapes as a pytest error with no console block or JSON report, and the final teardown then runs as though no iteration failed.

Not unrecreatable, contrary to earlier revisions of this list: the op jar or harness jar becoming unreadable mid-run surfaces as a nonzero subprocess exit, i.e. a `failed` iteration, and the run continues if the next reset succeeds; an `OSError` writing the JSON report is a printed warning and the report is not written, with the run's outcome unaffected; there is no port-bind check. Any other exception escaping an iteration (an unresolvable `${...}` in `properties:`, say) is a pytest error for the item, not a `failed` iteration.

Every `not_run` entry records which class fired and the underlying error, so a report never shows an unexplained gap.

## 11. Reporting

The runner produces console output and a stable JSON report.

Console output:

```text
Performance Test: myop-uppercase-names
Input: performance/customers.json (1,000 events)
JVM: -Xms512m -Xmx2g
Warmup: 1 replay | Measured runs: 3

Run Size: 100 replays
  Input Events:     100,000 (expected 100,000)
  Output Events:    100,000
  Median Throughput: 52,300 events/sec
  Duration:          1.913 sec median (1.902 / 1.913 / 1.941 min/med/max)
  CPU:               168% avg | 192% peak (of 1 core; 10 cores present)
  Memory:            220 MB peak RSS
  Correctness:       PASS (sampled, 1,000 of 100,000 records)

Run Size: 1,000 replays
  ...

Summary:
  Status: PASS
```

That is the all-success layout for an in-stream case. Variations, all additive: the header reads `Performance Test: <name> [<variant>, <k>=<v>]` for a fanned-out run; `JVM: (defaults)` when no `jvm:` block is set; a reader case prints `Source: N ticks per replay` in place of `Input:`, labels the count line `Ticks:`, its median `ticks/sec`, and adds `Emitted: N events/sec` (Section 4); a run size with failed iterations adds `Iterations: x of y succeeded` and one `iteration N FAILED: <reason>` line each; a run size cut short prints `NOT RUN: <class> (<error>)` (never started) or `NOT RUN (remaining iterations): ...`; when a JSON report was written, `Report: <path>` follows `Status:`. `n/a` stands in for any figure that could not be computed. The block always prints, even without `--perf-json`, and a failing item's `pytest.fail` summary repeats each failed iteration's reason.

The JSON report contains:

- `schema_version` (an integer, incremented on any breaking shape change; currently `1`);
- `test`: name, the `variants:` entry and `matrix:` permutation this report is for (`variant`, null when the case declares no `variants:`; `permutation`, an empty object when it declares no `matrix:`), `manifest_path`, and `perf_dir`;
- `input`: the authored `path` and `resolved_path` (both null for a reader), `events_in_file` (ticks per replay for a reader, with `replay_unit` set to `"tick"` or `"event"`), `expected_path`, and `expected_events`;
- `configuration`: the fully expanded performance configuration in effect (run sizes in both authored and integer form, warmup/measured counts, timeout, correctness mode, metric toggles); under `operator`, the configuration actually measured — `op_jar_ref` (the module reference, so named even for a UDF case), `properties:` with the permutation folded in (password-valued ones redacted), `requires:`, and `password_properties:` — self-describing, since two perf `test.yaml`s can share a `name:` while measuring different configurations, and reports are only comparable when this matches; under `jvm`, `min_heap`/`max_heap`/`args` and the `effective_args` the JVM received;
- `run_sizes[]`: per run size, `not_run` and `not_run_reason`, expected and actual input/output event totals, each iteration's status, duration, throughputs, warmup counts, CPU figures, memory figure, sample count, `insufficient_samples`, reset duration, correctness result (mode, `passed`, `records_compared`, `detail` — the first mismatch path when failed), and `failure_reason`; the Section 8 `aggregates`; the per-run-size `cpu`/`memory_rss_peak_bytes` summaries (Section 8); and a per-run-size `correctness` verdict (PASS only if every iteration that reached the check passed);
- `status`, the overall `PASS`/`FAIL`;
- `unrecreatable_environment` (`failure_class` and message, or null); and
- `metadata`, the Section 12 reproducibility metadata.

Writing the JSON report to disk is opt-in: it is written only when `--perf-json PATH` is given explicitly — a bare `pytest --perf` still builds the report and prints the console block, it just doesn't accumulate a JSON file per test under `.perf-results/` on every run. When `--perf-json PATH` is given, the report path is `PATH` itself when a single ITEM is selected, or `PATH/<UTC-ISO8601>-<key>.json` inside it when more than one is (`PATH` is then treated as a directory). ⚠ **Since `variants:`/`matrix:`, an item is a RUN, not a case**: `pytest --perf -k <a fanned-out case> --perf-json report.json` selects five items and therefore creates a DIRECTORY named `report.json`, not a file. Select a single node id to get a file. `<key>` is the perf test's own case path under `perf/`, flattened (e.g. `op-myop-myop-uppercase-names` for `perf/op/myop/myop-uppercase-names/`), not just its test name — two different perf cases can share a display name (a case's `name:` is free text) but never share a path. ⚠ **Not literally collision-free** — the filename applies a slug that collapses every run of non-`[A-Za-z0-9._-]` characters to one `-`, so `perf/op/x/case` under variant `postgres` and a sibling directory `perf/op/x/case-postgres` flatten together. The path-flattening half of that predates the run id (`a/b` and `a-b` already collided); it is documented rather than fixed. **A case declaring `variants:` or `matrix:` appends the run id** (`[postgres]`, `[postgres-UseUpsert=false]` — the same suffix as the pytest node id; a permutation VALUE has its `-`/`=`/`~` escaped as `~2d`/`~3d`/`~7e` and brackets turned to parentheses so the id stays injective) to that key, because one case path then produces one report PER RUN: without it the only thing separating two of them is `<UTC-ISO8601>` (basic form, `20260928T190100Z`), which is second-granularity, so two fast runs of the same case finishing inside one second would silently overwrite each other. The conventional destination is `scripts/integration/.perf-results/` (`.perf-results/` under `SLT_STATE_DIR`), which is generated output and is git-ignored.

## 12. Reproducibility metadata

Every report records, with `null` (plus a stated reason) for anything unavailable:

**Source control.** The runner resolves the work tree from the test directory rather than the process working directory — so it records your test repo, which tracks every operator's source and the perf cases — and records:

- repository root, current commit SHA, and branch;
- `dirty`: whether the work tree has uncommitted changes. A dirty tree means the commit SHA does **not** fully identify what was measured, and the report says so rather than implying reproducibility it cannot offer;
- `null` for all of the above, with an `unavailable_reason`, when git is unavailable or the directory is not a work tree.

The harness itself is NOT in that work tree: it comes from this repo's checkout at the commit your test repo's framework lock file names, if it keeps one. A tracked lock file means your repo's commit pins the intended framework commit transitively, but the report does not record the framework checkout's actual commit or dirtiness — only `framework_version`, the `inttest` package's version string (`0.1.0`, which does not change per commit). A framework checkout that has drifted from the lock is warned about at launch, not recorded in the report. Recording the framework commit is not implemented; stated so a reader does not take the git block for more than it covers.

**Build identity**, which source control alone does not cover, because the Striim install is external to the repository and the op jar is build-or-reuse:

- the resolved Striim release (version, series, Java release) and `STRIIM_HOME`;
- the built op jar's filename, size, and modification time;
- the jar's `Striim-Build-*` manifest fingerprint when present;
- whether the jar was rebuilt during this run or reused.

**Environment.**

- timestamp (UTC, ISO 8601, the item's start);
- hostname;
- operating system, release, and platform string;
- CPU count and total physical memory;
- the `java -version` string of the JVM actually used, and its path;
- Python version;
- integration framework package version (`framework_version`); the report's `schema_version` is at the top level, not here.

Each group's collector guards its own failures (bounded 10 s subprocess timeouts for `git`/`java -version`), so one group being unavailable never costs the other two or the report itself.

## 13. Directory structure

Performance tests live in their own tree in your test repo, `scripts/integration/perf/{op,udf}/<name>/<case>/`, parallel to `scripts/integration/regression/{op,udf}/<name>/<case>/` and never merged into it — the regression tree gains zero new files for a case that also has a performance counterpart:

```text
scripts/integration/regression/op/myop/
  myop-uppercase-names/
    test.yaml                      # op:, properties:, assert: -- unchanged, untouched
    input/
      customers.json               # integration input
    expected/
      customers.json               # integration expected output

scripts/integration/perf/op/myop/
  myop-uppercase-names/
    test.yaml                      # op:, properties:, performance: -- fully self-contained
    performance/
      customers.json               # performance input
      expected-customers.json      # performance expected output (one replay)
```

The perf root is always the sibling `perf/` of the regression root the engine collects (`SLT_INT_CASES`, default your test repo's `scripts/integration/regression`), so the framework checkout's location never moves it. Both trees use the same case-directory NAME (`myop-uppercase-names`) so the pairing is visually obvious, but that's purely a convention for humans — there is no mechanism linking the two `test.yaml`s at all. A performance test is its own `test.yaml`, structurally identical to a regression one except `performance:` replaces `assert:` (Section 3): `op:`/`properties:` (and any of `requires:`/`ddl:`/`seed:`/`disabled:`/`types:`/`password_properties:`/`udf:` a given operator or UDF needs) are authored directly, even when they happen to match the regression case's own values.

Every relative reference (a `ddl:`/`seed:` file, a `ConfigFile` property, `${TEST_DIR}`) resolves against the perf `test.yaml`'s own directory — there is only one directory involved. `performance.input`/`performance.expected` resolve there too, so performance-only fixtures live entirely under `scripts/integration/perf/`, never inside `regression/`.

Each behavior worth measuring is its own regression test directory with its own `test.yaml`, exactly as integration cases already are; a performance counterpart, when one is authored, is a separate, independently-authored directory under `perf/`. Variations of one operator (two modes, a scoped and an unscoped column selection) are expressed the way the integration tier already expresses them: as `properties:` values — including a `ConfigFile` pointing at a sibling configuration — in separate test directories.

## 14. Pytest and framework integration

### Options

`--perf`, `--perf-reverse` and `--perf-json` are added to the plugin's **existing** `pytest_addoption` function, which already registers `--slt-keep-resources`; a pytest plugin module declares that hook once.

```python
def pytest_addoption(parser):
    parser.addoption(
        "--slt-keep-resources", action="store_true", default=False,
        help="...",
    )
    parser.addoption(
        "--perf", action="store_true", default=False,
        help="run tests in performance mode (only tests under scripts/integration/perf/)",
    )
    parser.addoption(
        "--perf-reverse", action="store_true", default=False,
        help="ordering control: run the selected items in reverse order "
             "(same runs, same run ids; a UsageError without --perf)",
    )
    parser.addoption(
        "--perf-json", action="store", default=None, metavar="PATH",
        help="write the performance JSON report to PATH "
             "(if omitted, no JSON report is written -- only the console block prints)",
    )
```

(Help strings abbreviated; the plugin's are longer.)

### Selection

`testpaths` includes `"perf"` in both `pyproject.toml`s — this repo's (`["tests", "regression", "perf"]`) and your test repo's `scripts/integration/pyproject.toml` (for example `["tests", "regression", "perf"]`, loading the framework's plugin through `-p int_launch`) — so `scripts/integration/perf/` is collected at all. `pytest_collect_file` dispatches on file path rather than content — it must, because `testpaths` restricts only the *default* no-args collection root, so an explicit `pytest <dir>` (or a rootdir walk) can still reach a `test.yaml` the default run would never see. It gains a second branch: a `test.yaml` under `perf/` is collected as a distinct `PerfYamlFile`/`PerfYamlItem` pair (Section 3's shape — `performance:` in place of `assert:`, otherwise identical to the regression tier's own `test.yaml`), separate from `regression/`'s `IntYamlFile`/`IntYamlItem`.

Because `perf/` is a real collection root, `PerfYamlItem`s exist in the collected tree in BOTH modes, not only under `--perf` — so both directions of the collection-modification filter matter equally: with `--perf` set, everything is deselected except `PerfYamlItem`s (there is no `performance.enabled` to additionally filter on; a `disabled:` perf test.yaml is still selected here and skips itself, with a stated reason, at `runtest()` time); without `--perf`, every `PerfYamlItem` is itself deselected (they are never valid as ordinary integration tests — they have no `assert:` of their own to run). An empty remaining selection under `--perf` raises `pytest.UsageError` naming the roots searched and the number of `perf/**/test.yaml` files inspected.

Without `--perf`, selection and execution of everything under `regression/` and `tests/` are byte-for-byte unchanged from before `perf/` existed.

### Parallelism

Performance mode rejects pytest-xdist. The existing configuration guard already rejects `-n`/`--dist` unless the parallel opt-in environment variable is set; in performance mode the rejection is unconditional, with a message stating that measurements are not comparable under concurrent execution.

### Manifest handling

The regression tier's `test.yaml` loader (`manifest.load_manifest`) is completely untouched by performance mode: it has no `performance:` key to know about, because it never appears in a regression `test.yaml` (Section 3), and it is never called by performance mode at all. A separate loader (its own module, e.g. `perfmanifest.py`) owns the `perf/`-tree `test.yaml` shape, following the same style as `manifest.py` — typed frozen dataclasses, one `_normalize_*` function per key, errors raised naming the file and key — validating `performance:` (every rule in Section 3) plus `name:` and exactly one of `op:`/`udf:` (required) and the optional `properties:`/`purpose:`/`requires:`/`ddl:`/`seed:`/`timeout:`/`disabled:`/`types:`/`password_properties:`/`source:`/`target:`/`variants:`/`matrix:` keys, reusing `manifest.py`'s own private normalizers directly for the latter set (including `_normalize_op`/`_normalize_udf`/`_normalize_source`/`_normalize_target`/`_normalize_variants`/`_normalize_matrix`) so a perf `test.yaml`'s `properties:`/`op:`/`udf:`/etc. validate byte-identically to a regression one's. Perf-specific refusals on top of those: `performance.input` on a `source:` case, `source.expect_events`, and a `matrix:` value containing `${` (Section 4). `assert:` is rejected as an unknown key; the resulting `TestManifest` gets a permissive placeholder `assert_` (`AssertSpec(data=[], smoke=True)`) satisfying its shape, since no perf-mode consumer reads it.

A perf `test.yaml` written before the loader understands its shape fails loudly at load time (an unrecognized/malformed shape is a manifest error, same as a malformed regression `test.yaml`) rather than being silently ignored.

File existence is not checked by either loader, by the same documented policy `manifest.py`'s loader already follows; those checks are the runner's pre-flight (Section 3) instead.

### Operator subprocess

Performance mode launches `java <jvm args> -cp <harness-jar>:<op-jar>:<extra driver jars> com.striim.testing.inttest.PerformanceProcessor <request.json>` with the test directory as working directory — the same classpath shape as integration mode, including the extra JDBC driver jars the regression harness adds (a Teradata variant failed here with "No suitable driver found" before they were shared). The configured `jvm:` options go before `-cp`; nothing else is added — in particular no `-Duser.timezone`, which the regression harness does pass (Section 4, "Writers"). The launch waits up to `performance.timeout`, then kills the process tree (Section 10). The working-directory contract is preserved: the working directory is the perf `test.yaml`'s own directory (Section 3), so relative configuration-file references resolve there.

### Dependencies

Process CPU and memory sampling requires `psutil`, a declared dependency of the framework's `inttest` package (`psutil>=5.9`). Pre-flight requires it unconditionally — not only when `capture_cpu`/`capture_memory` are enabled — because the process-tree kill and survivor check (Section 10) need it too. When it is unavailable, pre-flight fails with an explicit message; it never silently reports zeros or silently disables metrics.

## 15. What this framework does not measure

The unit under test is the `Processor` (or a UDF's functions) as a whole, measured from outside the process it runs in. It trades measurement granularity for one YAML-driven mechanism shared with the integration tier: correctness validation against expected output, CPU and resident-memory metrics, and a machine-readable report with reproducibility metadata.

These measurements are out of scope, by design:

- **JVM heap footprint and bytes-per-entry accounting.** Retained-heap measurement of an operator's internal data structures.
- **Capacity-limit-stop semantics.** Halting a run when the projected working set would exceed a fraction of the configured heap, instead of running to an out-of-memory error.
- **Lookup latency isolated from key-construction cost.** Reporting a raw internal-access cost separately from the realistic per-event cost.
- **Side-by-side backend or store comparisons.** Running one workload against two interchangeable internal implementations and reporting comparative figures.
- **Generator-only baseline subtraction.** A second timed loop that produces input but performs no processing, so loop and allocation overhead can be subtracted from the result.
- **In-process, zero-allocation microbenchmarking.** Constructing the operator in the test JVM and reusing a single mutable event across iterations to eliminate allocation from the timed loop.

Every one of these measures something inside the `Processor` — a store implementation, a key builder, an input generator, a heap-resident collection — or requires the benchmark to run in the same JVM as the test in order to reach it. Both are outside this framework's boundary: the unit under test is the `Processor` as a whole, invoked exactly as the integration tier invokes it, measured from outside the process it runs in (Section 1).

An operator that genuinely requires that level of introspection is not served by this framework, and no extension point is provided to bolt it back on. Per-operator custom phases and custom metrics are precisely the bespoke complexity this replacement removes; adding a hook for them would reintroduce it under a new name.

## 16. Dependent tooling

Your test repo's own tooling (its runners, checks and reports)
builds on the convention this spec describes, and nothing else: the `--perf`, `--perf-reverse` and
`--perf-json` options (Section 2), the self-contained parallel `scripts/integration/perf/<kind>/`
tree (Section 13), the Section 11 console block and the structured `--perf-json` report.

## 17. Not supported

The following are not supported:

- randomized or generated input;
- multiple named performance scenarios within one `test.yaml`;
- per-record latency and percentile latency;
- content validation of every replay rather than the compared replay (it requires per-replay expected outputs);
- distributed metrics;
- JVM GC and heap metrics;
- threshold-based assertions (failing a run on a throughput regression); and
- historical regression tracking across runs.
