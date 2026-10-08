# Integration tests: an Open Processor or UDF without a Striim app

The integration tier drives your module's code directly: it builds the jar, loads it into a small
Java harness, feeds it events from a JSON file, and compares what it emits with the events you
expect. There is no Striim server and no TQL app, so a case runs in seconds. Use it for the
behaviour of the code itself; use a live test ([TESTING-YOUR-JAVA.md](TESTING-YOUR-JAVA.md)) to
prove the module works inside a running app.

## What you need

- Docker, when a case `requires:` a database or emulator;
- Python 3.12 and this framework installed (as in [RUN-YOUR-FIRST-TEST.md](RUN-YOUR-FIRST-TEST.md));
- Maven and a JDK 17 (11 for Striim 5.0 and 5.2), and `STRIIM_HOME` set to a Striim install: the
  framework compiles your module and its own harness against the jars in that install. No Striim
  server runs.

## Where cases live

This repo ships no integration cases. Yours live in your own repo, one folder per case:

```
tests/integration/
  op/<module>/<case>/test.yaml
  udf/<module>/<case>/test.yaml
```

Point the framework at them with `suites.integration` in your `gold-targets.yaml`
([SET-UP-YOUR-OWN-REPO.md](SET-UP-YOUR-OWN-REPO.md)), or with `SLT_INT_CASES`. Run them with:

```
striim-test run --targets gold-targets.yaml --tier integration
```

`--case <id>` runs one; `--parallel` runs several at once.

## Your first case

This case drives the sample Open Processor (`samples/code/op`), which copies each event and adds
`processed=true` to its userdata. One input event, one expected event:

```
referenceop-adds-processed/
  test.yaml
  input/events.json
  expected/events.json
```

<!-- snippet: shape an integration-tier test.yaml -->
```yaml
name: referenceop-adds-processed
purpose: every event is copied unchanged, with userdata.processed set to true
op:
  jar: samples/code/op/java/OpenProcessors/ReferenceOp
assert:
  data:
    - input: input/events.json
      match: expected/events.json
```

`input/events.json`, one event as a WAEvent-JSON object:

```json
[{"metadata": {"TableName": "SRC.ITEMS", "OperationName": "INSERT"},
  "data": {"values": [1, "apple"], "present": [true, true]},
  "userdata": {}}]
```

`expected/events.json`, the same event with the stamp:

```json
[{"metadata": {"TableName": "SRC.ITEMS", "OperationName": "INSERT"},
  "data": {"values": [1, "apple"], "present": [true, true]},
  "userdata": {"processed": "true"}}]
```

- `jar:` is the Maven module folder, relative to your project root. The framework runs
  `mvn package` there when the jar is missing or was built for another Striim release.
- An event has `metadata` and `userdata` maps, and `data` (and `before`, for an update) as
  `values` plus `present`. `present` says whether the source supplied each column, so `null`
  and "not supplied" are different.
- Events compare in order, and the first difference is reported: which event, which section,
  which column or key, expected against actual.
- Write the expected events by hand from the input and what the code should do, never by
  copying what it emitted.

## Rules

- **One session per checkout.** A second integration run in the same checkout is refused while
  one is running, because serial runs share table names. Use `--parallel` for concurrency inside
  one run.
- **The tier builds your module.** Do not run your own `mvn package` in the same module while a run
  is in progress: two builds in one `target/` folder fail in confusing ways (`NoSuchFileException`
  on class files that exist, or a run that reports one case fewer than it should).
- **Services** are the same as the live tier's, on other ports so both tiers can run at once
  ([SERVICES.md](SERVICES.md), "Integration tier").

## Performance runs

A perf case is a separate `test.yaml` in a parallel `perf/` tree beside your integration cases,
with `performance:` in place of `assert:`. It replays the input at increasing sizes and reports
throughput, CPU and memory; `--perf-json PATH` writes the report as JSON. The full specification is
[internals/PERF_SPEC.md](internals/PERF_SPEC.md).

# Reference

## Case layout and `test.yaml`

One case per directory: `regression/{op,udf}/<module>/<case>/`, holding a `test.yaml` plus its
`input/*.json` and `expected/*.json` WAEvent-JSON fixtures (compact form — one JSON object per
line). Tests are YAML-driven; authors write no Python.

```
regression/op/myop/myop-uppercase-names/
├── test.yaml
├── input/customers.json
└── expected/customers.json
```

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
name: myop-uppercase-names     # required; unique test id
purpose: >                                  # optional; what this case pins
  uppercase mode uppercases every string column
op:                                         # exactly one of op:/udf: is required
  jar: java/OpenProcessors/MyOp            # repo-relative MODULE ref (dir or pom.xml)
properties:                                 # optional; passed to the constructed Processor
  Mode: 'uppercase'
assert:                                     # required
  data:
    - input: input/customers.json
      match: expected/customers.json
```

Remaining optional keys: `password_properties:` (names `properties:` keys to wrap in `Password`),
`requires:` (services: `postgres`, `mysql`, `oracle`, `sqlserver`, `spanner`, `gcs`, `vertica`, or a
connection-only `teradata`/`servicenow`; see [SERVICES.md](SERVICES.md)), `ddl:`/`seed:`
(a filename, or a `{file, db}` list — `db` defaults to `postgres-source`; other routes are
`oracle-source`/`spanner-google`/`spanner-postgres`, …), `types:` (source schemas by `TableName`, for
`TypeResolver`-consuming ops — see below), `source:` (drive the op as a reader — see below),
`target:` (drive it as a Striim Target — see below),
`timeout:` (seconds, default 120), `disabled:` (truthy => skip, use a
string reason). `assert.smoke: true` substitutes for `assert.data` when the case only proves the
operator constructs and runs.

**`types:`** declares the source schema for each `metadata.TableName` an operator will see:
column names, in the order they occupy `data[]`/`before[]`. The harness mints a type UUID per
declared table, stamps it onto matching input events, and serves it back through both the
`BuiltInFuncs` seam (field/alias introspection) and the `TypeResolver` seam (type resolution) —
so one block feeds everything a type-consuming core asks for. A table with no entry resolves to
`null`, which is what leaves a core's create-if-absent path intact.

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
types:
  SRC.CUSTOMERS: [customer_id, first_name, email]      # columns only

  SRC.ORDERS:                                          # ...or with the metadata a type carries
    columns: [order_id, customer_id, total]
    keys: [order_id]                                   # key columns
    aliases: { total: TOTAL_AMOUNT }                   # display aliases
```

Reach for the mapping form when the operator **reads** that metadata — one that emits DDL or
propagates keys takes its no-primary-key branch otherwise, silently, and the case certifies less
than it appears to. A `keys:`/`aliases:` entry naming a column the table does not declare is a
manifest error, not a no-op.

**`assert.data[].ignore_fields:` / `.project:`** narrow the comparison for an operator that stamps
something non-deterministic into its output — a wall clock, a generated id — which is otherwise
un-assertable, since `assert.data` is exact-match. `ignore_fields` drops the named fields;
`project` compares *only* them. They are inverses and **declaring both is an error**. Each takes a
field path (or a list of them) in exactly four forms — `metadata.<key>`, `userdata.<key>`,
`data[<i>]`, `before[<i>]`. **No wildcards**, deliberately: `metadata.*` is how a projection turns
into "stop asserting", and an op with an unbounded set of volatile fields has a determinism
problem the harness should not paper over.

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
assert:
  data:
    - input: input/events.json
      match: expected/events.json
      ignore_fields: ["metadata.ReadTime", "data[7]"]     # or: project: ["data[0]", "data[1]"]
```

Three behaviours worth knowing before you use them.

1. **The two keys are not mirror images.** `ignore_fields` says *this field is volatile*: a
   `data[i]` entry suppresses the **value** check only, and `present[i]` is still compared,
   because a wall-clock column varies in value and never in presence — and the presence bitmap is
   where event-copy bugs show. `project` says *only these fields are deterministic*:
   everything else is out of scope entirely, value **and** presence, since an operator whose other
   columns vary in shape could not use a projection at all otherwise.
2. **A path that matches no EMITTED event fails the case.** Both keys weaken an assertion by
   construction, so a path that has stopped matching — a renamed key, a reordered column — must
   not pass silently. The check is against the emitted array specifically: if the path matches
   only your *expected* fixture, the operator has **stopped emitting** that field, which is a real
   change and is reported as one rather than ignored.
3. **An entry applies to every event in the array**, while reachability needs only one. Ignoring
   `data[1]` because event 0 carries a clock there also stops comparing column 1 in every other
   event — so scope a fixture to the events that need it rather than reaching for a broader
   ignore.

`assert.gcs_objects:` (a bucket-relative object key, or list of them —
never a `gs://` URI) supplements `assert.data` with a post-run check that each key genuinely exists in
`${GCS_BUCKET}`; it requires `requires: [gcs]` and is `${...}`-token-rendered like everything else
here. Every relative path resolves against the case directory; `${TEST_DIR}`
(plus `${TID}`/`${TID_ORACLE}` and each `requires:` service's connection tokens) renders inside
`properties:` values.

**`assert.jmx:`** supplements `assert.data` on an `op:` case with the op's MBean attributes after
its events are processed:

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
assert:
  data: [...]
  jmx:
    attributes:
      Hits: 2                 # exact: number, true/false or string
      Misses: {min: 1}        # range: min and/or max
      GateRunning: false
    bean: MyOpMBeanView            # optional; simple name (core package) or fully qualified
```

- The runner never builds the op's `App`, so it builds the bean itself (`JmxSnapshot.java`):
  after the last event and before `close()`, it finds the single public class in the core's
  package that implements an `*MXBean`/`*MBean` interface and has a public constructor taking the
  core `Processor` followed only by `Supplier` parameters. Each `Supplier` gets `() -> null`, so
  state the `App` owns reads as absent. More than one candidate needs `bean:`.
- It registers the bean through the op jar's shaded `OpJmxRegistry`, as production does, under
  `com.example:type=<Type>,name="inttest.source"`, reads every attribute, then unregisters.
  The snapshot is a sidecar file; the event output is unchanged.
- A bean that cannot be built or registered, an attribute that does not exist, and a getter that
  throws all fail the case, with the full snapshot in the message.
- Refused at load: unknown keys, empty `attributes`, attribute names that read a clock or
  duration (`…Millis`, `…Time`, `…Latency`, …), and `jmx` without `data`, with `expect_error`, or
  on a `udf:`/`source:`/`target:` case.
- Evaluated once per drive: each `data` entry and each `matrix:` permutation is a fresh JVM with
  fresh counters.

### Execution model

`inttest/plugin.py` collects each `test.yaml` (`pytest_collect_file` → `IntYamlFile`/`IntYamlItem`),
`mvn package`s the module jar if it is missing or built against a different Striim release,
provisions/resets any `requires:` services, then runs the shared Java harness
(`java/`, `com.striim.testing.inttest.IntegrationProcessor`) as a subprocess. The harness loads the jar
in a child classloader, reflectively constructs the op's `Processor` via its canonical
`OpenProcessorCommon` constructor — injecting `MockBuiltInFuncs`/`MockTypeResolver` by parameter
type — feeds it each input `WAEvent` through `EventProcessor.processEvent`, and serializes every
emitted event back to JSON for comparison against `expected/`. No live Striim server and no Striim
install on the classpath at run time (only to build the jar).

## Feeding an Operator Whose Input Is Not a `WAEvent` (`kind: avro`)

Most in-stream ops are fed a `WAEvent`. An op that converts a **wire format** is fed that format's
own event type instead. An op that converts Debezium Avro, for example, takes a `com.webaction.proc.events.AvroEvent`, which
extends `SimpleEvent`, is **not** a `WAEvent`, and carries an
`org.apache.avro.generic.GenericRecord` payload. Nothing in the case's shape changes: it is still
an `op:` case with `properties:` and `assert.data`. Only the input fixture's `kind` differs.

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
name: myavroconverter-insert
op:
  jar: java/OpenProcessors/MyAvroConverterOp
assert:
  data:
    - input: input/insert.json
      match: expected/insert.json      # expected/ is ordinary WAEvent-JSON: the op EMITS WAEvents
```

```json
[
  {
    "kind": "avro",
    "schema": { "type": "record", "name": "Envelope", "fields": [
      { "name": "op", "type": "string" },
      { "name": "source", "type": { "type": "record", "name": "Source", "fields": [
          { "name": "table", "type": "string" } ] } },
      { "name": "after", "type": ["null", { "type": "record", "name": "Value", "fields": [
          { "name": "id", "type": ["null", "int"], "default": null } ] }], "default": null },
      { "name": "before", "type": ["null", "Value"], "default": null } ] },
    "record": { "op": "c", "source": { "table": "customers" }, "after": { "id": 1 } },
    "metadata": { "KafkaRecordTimestamp": 1730000000000 },
    "userdata": {}
  }
]
```

- **`schema`** is Avro's own JSON schema syntax, parsed by Avro. A named record can be **reused by
  name** in a later field (`before` above), which is how Debezium emits an envelope.
- **`record` is PLAIN JSON, not Avro's JSON encoding.** Avro would demand
  `{"after": {"Value": {"id": {"int": 1}}}}` — every field wrapped in its union branch. A fixture
  nobody can read is a fixture nobody can check against what the case claims, so the value is
  written the way the data looks and resolved against the schema: a union takes its null branch for
  a JSON null, otherwise the first branch the value fits.
- **`metadata`/`userdata`** land on the `AvroEvent` envelope, not inside the record. This is where a
  Kafka-supplied value like `KafkaRecordTimestamp` goes — **supply it** if the operator reads one,
  or the op falls back to a wall clock and your assertion is not reproducible.
- **A field the schema does not declare is an error**, not a dropped key: a typo'd fixture field
  otherwise reads as "the operator ignored my column", which is a real defect's symptom.
- **There is no avro OUTPUT.** `expected/` is always WAEvent-JSON, because an op taking an
  `AvroEvent` emits `WAEvent`s. `kind: avro` is input-only, and the WAEvent-only reader rejects it
  rather than mis-reading it.
- **A `udf:` or `target:` case cannot take one** — both are fed Striim stream events — and each
  says so by name rather than failing as a cast.

Two mechanics worth knowing if you are extending this. The harness resolves `processEvent(WAEvent)`
first and by exact match (it is the `EventProcessor` contract, and a core implementing
`EventProcessor<WAEvent>` also carries javac's synthetic `processEvent(Event)` bridge, which a
widest-match search would be free to pick); only when there is no such method does it fall back to
the single one-argument `processEvent` the core declares. And ⚠ **a core declaring
`processEvent(Object)` cannot be pre-checked** — every event is assignable to it — so a fixture of
the wrong kind is reported by the operator's own cast rather than by the harness. That message is
clear, but do not expect the harness's `check the fixture's kind` hint on such an op.

## Driving a Bare UDF (`udf:`)

This applies to a `test.yaml` in EITHER tree — `scripts/integration/regression/udf/<module>/<case>/`
or its `perf/udf/<module>/<case>/` counterpart (see "Performance Testing" below) — since `udf:` is a
`test.yaml` schema key, not a perf-only feature; a UDF's regression fixtures are almost always
authored first.

`op:` and `udf:` are sibling, mutually exclusive top-level keys — exactly one is required. `op:
{jar: ...}` drives the module as an OpenProcessor's `Processor`, exactly as above. A `udf:` block
is fully self-sufficient (its own `jar:`, no `op:` block at all) and drives the module instead as
a bare UDF pipeline — a sequence of `public static` method calls, with no `Processor` and no
constructor involved (`properties:`/`password_properties:` are rejected outright in a `udf:` case,
since neither has any meaning for a bare static function):

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
udf:
  jar: java/UserDefinedFunctions/MyUdf
  class: com.example.MyUdf            # fully-qualified UDF class inside udf.jar
  kind: waevent                       # "waevent" (default) or "jsonnode"
  # source/target (jsonnode only, default data[0]): the WAEvent-JSON envelope slot
  # (data[N] / before[N] / userdata.KEY) the "$" register is read from / written to.
  pipeline:
    - function: MySetLogging          # simple (unqualified) static-method name
      args: [false]
      as: _log                        # binds the result to a name INSTEAD of updating "$"
    - function: MyTransform
      args: ['$', CUSTOMER, 'uppercase']
```

Each pipeline step threads one value through a `$` register: a step without `as:` updates `$`
with its result; a step with `as:` binds the result to that name (retrievable by a later step's
`{ref: name}` arg) and leaves `$` untouched — this is what lets a side-effecting toggle like
`MySetLogging` sit inside a pipeline without breaking the chain. `kind: waevent` (the default)
starts `$` as the input `WAEvent`; the pipeline's final value IS the emitted event (no emission
when it ends `null`). `kind: jsonnode` starts `$` as the `JsonNode` parsed from one envelope slot
(default `data[0]`) — the WAEvent-JSON fixture format is unchanged either way, a `jsonnode`
case's `data[0]` is simply a JSON-document string, the same shape a production CQ hands
a JSON function via `JSONParse(TO_STRING(data[0]))` — and the final value is serialized back into
a (possibly different) slot of the same event, which is then emitted.

Each arg is one of: the literal string `'$'` (the current register value); `{ref: name}` (an
earlier step's `as:` binding — forward and self references are rejected at load time);
`{json: ...}` (parses a JSON-document string, or converts an already-structured YAML
value, into a `JsonNode`); `{str: text}` (a literal string, escaping the `'$'` shorthand or a
number-shaped string); any other scalar (string/bool/number); or a YAML list of any of the
above (feeds e.g. `JSONBuildArrayFromList(List)`). Method resolution prefers an exact-arity
non-varargs overload over a varargs one, mirroring the dispatch a CQ's generated code would
perform. So give a UDF concrete non-varargs overloads for its common call shapes.

## Driving a Reader (`source:`)

An in-stream op is *fed*: one `processEvent(WAEvent)` per input event. A reader has no input at
all — it PULLS from its source and PUSHES into a channel, so its core method is `tick(<channel>)`.
A `source:` block says "drive this op that way". It is not a sibling of `op:`/`udf:`: the case
still names its module with `op: {jar: ...}`, and `source:` alongside `udf:` is rejected (a UDF is
a bare static function; there is no tick to drive).

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
name: myreader-initial-load
op:
  jar: java/OpenProcessors/MyReaderOp
requires: [spanner]
source:
  max_ticks: 20                     # required: the tick BUDGET (integer >= 1)
  expect_events: 12                 # optional: stop once this many events are collected
  seed_when: post_start             # optional: commit `seed:` AFTER the reader's first tick
assert:
  data:
    - match: expected/events.json   # no `input:` -- a reader is not fed
      sort_by: [ "data[0]" ]        # optional: line events up by key, for set-shaped output
```

- The harness calls `tick()` once per step, handing the core a `Proxy` over **its own** channel
  interface — discovered from the `tick` signature, never named in the YAML, because each op
  shades its classes into a module-unique package that every version bump moves.
- **`expect_events` absent** ⇒ exactly `max_ticks` ticks are driven and everything they emit is
  collected. This is the form a case proving a source stays *quiet* needs.
- **`expect_events` set** ⇒ ticking stops as soon as that many events have arrived; a budget
  exhausted below it fails naming both numbers, rather than leaving a half-delivered stream to
  surface as a WAEvent mismatch that reads like an op defect. A tick is atomic — one that
  overshoots keeps what it emitted instead of truncating to the expectation.
### `seed_when: post_start` — committing data while the reader runs

`ddl:` and `seed:` normally run **before** the operator is driven, which is right for a snapshot:
it reads what is already there. A **change stream** is the opposite — it captures only commits made
*after* its start timestamp — so seeding beforehand is invisible to it by construction, and such a
case sits at zero events looking exactly like an operator bug.

`source: {seed_when: post_start}` holds `seed:` back until the reader has taken its **first tick**,
which is the tick that opens the stream query and fixes its start timestamp. The two sides rendezvous
on files in a shared temp directory: the driver writes `seed.ready` and blocks, this side runs the
seed SQL and writes `seed.go`.

- **Exactly one `assert.data` entry** is allowed with `post_start`, and a second is rejected at
  manifest load. Each entry drives the reader again in a fresh temp directory while the seed has
  already been committed, so a later drive would see an empty source and fail on the event count.
- ⚠ **This is the one place a reader case has a wall clock.** The handshake has a timeout, derived
  from the case's own `timeout:` so the Java side always gives up first and reports the handshake
  rather than letting a generic "timed out" blame the operator. It bounds a *handshake*, never an
  assertion — the tick budget is still the only thing that decides a case's outcome.
- The **live** tier has a similarly-named `seed_when`, reworked into a per-file `when:` with an
  `after:` delay. The two tiers are not the same feature: this one is a case-level flag with a
  blocking handshake, because a subprocess driver has no "app is RUNNING" moment to hang a delay on.

### `sort_by:` — comparing output that is a SET, not a sequence

`waevent.compare` is positional. Some readers emit a set: a snapshot read with `SELECT *` has no guaranteed row order
without an `ORDER BY`. `sort_by:` orders
**both** sides by the named paths before comparing.

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
assert:
  data:
    - match: expected/events.json
      sort_by: [ "data[0]", "metadata.TableName" ]
```

- Paths use the same grammar as `ignore_fields`/`project` (`metadata.<key>`, `data[<i>]`, …). Note
  the **quoting**: `sort_by: [data[0]]` is a YAML parse error, since `[` is flow-sequence syntax.
- Chosen over a bare `unordered: true` deliberately. A multiset compare needs no key — and cannot
  say WHICH event differs, only that the bags do, and it lets a case stay green while an operator's
  output order becomes genuinely arbitrary. Naming the key keeps the diff positional.
- A key that **does not discriminate** is rejected: a repeated key would pair events by fixture
  order, which is the comparison `sort_by` exists to replace.
- A key that matches nothing on **either** side is rejected too — it would sort that side by a
  constant, silently restoring the positional comparison.
- ⚠ `sort_by` can hide an ordering regression. If every case sorts, nothing asserts that the
  operator orders at all. Keep at least one case per reader comparing positionally.

- **`assert.data[].input` is rejected, not ignored**: a reader pulls, so an input fixture would
  never be read, and its author would be asserting against events they believe they supplied.
- **`source:` requires `assert.data`.** A smoke-only case never drives the operator at all, so the
  reader would be ticked zero times and the block would sit dead.
- **There is deliberately no timeout or wait key.** The budget is a COUNT, so a slow machine takes
  longer to run the case but cannot change its outcome. A wall-clock predicate would make the
  machine a participant in the assertion, and the case flaky.

What the source emits is not declared here. A scripted source is code, and it reaches the core
either through a `requires:` service (a seeded emulator) or through the op's own
`IntegrationSeams` class, below.

**`source:` works in the perf tree too** ([internals/PERF_SPEC.md](internals/PERF_SPEC.md) §4). There a replay is
`source.max_ticks` ticks instead of one pass over `performance.input`, which is likewise rejected.
One extra rule applies: the source must be **replay-stable** — the same number of events every
`max_ticks` ticks, indefinitely — because the operator is constructed once and replayed. A source
that drains is caught by the existing emission-count stability check, naming the replay where it
ran dry. `expect_events` is rejected in a perf case for the same reason: every replay must be
exactly `max_ticks` ticks.

## Driving a Target (`target:`)

An in-stream op is *fed* and emits events; a reader is *ticked* and emits events. **A target is fed
and emits nothing.** Its observable output is three things that are not a returned list: the
**target database**, the **checkpoint row**, and **what it acknowledged**. That is why it takes a
new seam (`TargetDriver`) rather than a fourth `EventDriver`, and why it has its own `assert:`
keys. The case still names its module with `op: {jar: ...}`; `target:` alongside `source:` or
`udf:` is rejected.

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
name: mywriter-restart-does-not-double-write
op:
  jar: java/OpenProcessors/MyWriterOp
requires: [postgres]                  # required: a target with no real database asserts nothing
ddl:
  - file: ddl_customers.sql
    db: postgres-target
types:
  SRC.CUSTOMERS: [CUSTOMER_ID, FIRST_NAME, EMAIL]
properties:
  ConnectionURL: '${POSTGRES_URL}'
  Username: '${POSTGRES_TARGET_USER}'
  Password: '${POSTGRES_TARGET_PASSWORD}'
  Tables: 'SRC.CUSTOMERS,${POSTGRES_TARGET_SCHEMA}.${TID}customers'
password_properties: [Password]
target:
  input: input/customers.json         # required: a target IS fed, unlike a reader
  restart_after: 3                    # optional: stop and resume after N events
  mid_run:                            # optional: SQL run between events, at gate `after`
    - {after: 2, file: wait.sql, db: postgres-target}  # db defaults to the target's route
    - {after: 2, file: probe.sql}     # steps sharing an `after` run in list order
  positions: true                     # optional, default true; false = the no-recovery path
  distribution_id: inttest            # optional
  timezone: Asia/Tokyo                # optional: run the driver JVM at this -Duser.timezone
assert:
  target:                             # the database the writer wrote
    - query: >
        SELECT CUSTOMER_ID, FIRST_NAME FROM ${POSTGRES_TARGET_SCHEMA}.${TID}customers
        ORDER BY CUSTOMER_ID
      db: postgres-target             # optional, defaults to postgres-target
      match: expected/customers.json  # a JSON list of rows, each row a list of values
  acked: 5                            # optional: how many events it acknowledged
  restarts: 1                         # optional: how many times the writer was restarted
  replayed: 3                         # optional: how many events were fed a second time
```

- **`target.input`, not `assert.data[].input`.** A target emits nothing, so there is no
  `{input, match}` pair to hang the input on — `assert.data` is *rejected* for a target case
  rather than silently comparing against an empty list.
- **`assert.target[].query` must carry an `ORDER BY`**, checked at load. Rows are compared in
  order, and a database may return them in any order it likes; without one the case is green on
  the machine that recorded the golden and arbitrary everywhere else.
- **`match` is a list of rows**, each a list of column values, in the shape `dbroutes.query_rows`
  returns. Values are normalised losslessly: `Decimal` → string (never a float — a writer that
  refuses a value not fitting a column's scale is asserting about exact digits), temporals → ISO
  8601 keeping any offset, `bytes` → lowercase hex.
- **To FEED bytes, use `{"$hex": "00ff"}`** in an input fixture's `values`. JSON has no byte type,
  so without it every value a case could feed was a string, a number, a boolean or null — binary
  was inexpressible rather than untested. It must be an object with that reserved key: a bare
  string cannot serve, because strings are legitimate values and guessing "this one looks like
  hex" would turn a CHAR column holding `"cafe01"` into bytes. Decoding is strict — an odd length
  or a non-hex digit is refused, not truncated. It pairs with the `bytes` → lowercase hex rule
  above, so a binary column is stated the same way on both sides of a case.
- **`restart_after: N` is the reason the tier exists.** After N events the writer is closed and
  rebuilt from the same properties. `close()` deliberately does not flush, so anything still
  accumulated was never applied and never acked — the driver then reads what the writer reports
  as durable and **resumes the stream from exactly there**, so unacked events replay. A writer
  that double-writes on replay shows it in the target table. It requires `positions: true`:
  with no positions there is nothing to checkpoint, so the restart would replay everything and
  the case would be asserting idempotence rather than recovery.
- **`positions: false`** drives the no-recovery path, where every event arrives with a null
  position and the writer's `isRecoveryEnabled` is false (RECOVERY off). Not a curiosity — it is
  the path a live app runs unless recovery is on.
- **`timezone:` is how a temporal defect becomes visible at all.** The live stack runs UTC, and a
  unit test with a mock `PreparedStatement` sees only what is handed to the driver — a
  `timestamptz` conversion happens *inside* it. Pair a case with its own twin at an offset zone and
  have both compare against **one** `expected/` fixture: a stored instant is the same instant
  whatever zone the writer ran in, so if the two expectations ever have to differ, that difference
  is the defect. It is a JVM argument, not an env var — a driver reads the default zone during its
  own class initialisation, so setting it later is already too late. An unknown zone is rejected at
  load, because the JVM would silently fall back to GMT and turn the case into a second UTC one.
- **`assert.restarts:`/`assert.replayed:` are what make a recovery case about recovery.** A
  `restart_after:` case writes the same rows and acks the same count as one with no restart, so
  those two numbers are the only thing that says the restart happened. They do **not** satisfy the
  "a target case must assert something" rule on their own: they describe what the *harness* did,
  not what the writer wrote.

### `matrix:` — one case, every permutation, one answer

The behavioural flags **must not change the answer**. A `matrix:` block cross-products them and
runs the case once per combination, all against its **single** `assert:` block:

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
target:
  input: input/customers.json
matrix:
  UseUpsert:          ['true', 'false']
  CompactEvents:      ['true', 'false']
  NormalizeColumnSet: ['true', 'false']       # => 8 runs, 1 expectation
assert:
  target:
    - query: SELECT ... ORDER BY id
      match: expected/customers.json
  acked: 5
```

- **The single shared expectation is the point, not an economy.** It states the invariant directly
  — *these knobs are not observable in the output* — where eight transcribed expectations could
  each drift independently and still look green. If one permutation ever needs its own answer,
  that difference **is** the defect.
- **What may legitimately differ is performance**, which this tier does not measure and must not
  start to: `CompactEvents: false` is slower by design, `NormalizeColumnSet: true` costs a full row
  per update. That belongs to the perf tier.
- **Each permutation gets a clean database** — `ddl:` and `seed:` re-run between them — so the
  shared expectation is what each one produced, not the union.
- **Every failure names its permutation**: `[UseUpsert=true, CompactEvents=true, …]`. With eight
  runs behind one assertion, a mismatch that did not say which combination produced it would send
  the reader off to re-run them by hand.
- A key may not appear in both `properties:` and `matrix:` (which wins is not something an author
  should have to know), and a value list needs at least two distinct entries.
- **It is not target-only.** An OpenProcessor or reader case can matrix over its own
  `properties:` the same way. Only a `udf:` case is refused: a UDF is a bare static function with
  no `properties:`, so every permutation would be identical.

### `assert.expect_error:` — proving a REFUSAL

Some outcomes are refusals, and a refusal has no output to compare and no rows to read back. This
writer refuses a decimal that will not fit its column's scale, a temporal value it would otherwise
have to fabricate, a table name that matches two tables. `expect_error:` is how a case pins one:

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
assert:
  expect_error: 'matched 2 tables'      # the drive MUST fail, and say this
```

- **The drive must fail.** Succeeding fails the case, and says so: *"expected the drive to be
  REFUSED … but it SUCCEEDED. A refusal that stopped happening is a behaviour change."*
- **And fail for the stated reason.** A different failure is reported as *"failed as expected, but
  not for the stated reason"*, quoting both.
- **A substring broad enough to match anything is rejected at load** — `Error`, `exception`,
  `failed`. A case that cannot tell the refusal it is testing from the harness failing to start is
  worse than no case.
- **It cannot be combined with `data:`, `acked:`, `monitor:`, `restarts:`, `replayed:`,
  `exception_store:` or `jmx:`.** A failed drive writes no run report and emits nothing, so those
  have nothing to read.
- **`target:` is allowed, and is how a rollback is proved.** The database survives the failure:
  query the table and the checkpoint row to show the failing window landed nothing.

### `variants:` — one case, every database type

The knob matrix cross-products *independent* flags. An **engine** is not independent: its URL,
user, password, provider type, DDL and routes all move together as one named choice — a
cross-product would pair a PostgreSQL URL with an Oracle password. So engines are **named
variants**:

<!-- snippet: shape integration-tier test.yaml keys -->
```yaml
properties:                        # ONE properties block for every engine
  ConnectionURL: '${V_URL}'
  DatabaseProviderType: '${V_PROVIDER}'
  Tables: 'SRC.CUSTOMERS,${V_TABLE}'
variants:
  postgres:
    db: postgres-target            # the route this variant's assertions read through
    ddl: [ddl_pg.sql]              # its own SQL, run through its own route
    tokens:
      V_URL: '${POSTGRES_URL}'
      V_PROVIDER: 'Postgres'
      V_TABLE: '${POSTGRES_TARGET_SCHEMA}.${TID}customers'
  oracle:
    db: oracle-target
    ddl: [ddl_ora.sql]
    tokens: {V_URL: '${ORACLE_URL}', V_PROVIDER: 'Oracle', V_TABLE: '…${TID_ORACLE}CUSTOMERS'}
assert:
  target:
    - query: 'SELECT ${V_COLS} FROM ${V_TABLE} ORDER BY 1'   # ONE query SHAPE
      match: expected/customers.json                         # ONE expectation
```

- **A variant supplies token values, its own `ddl:`, and its route — and nothing else.** The
  `properties:` block, the query shape and the expected fixture are authored **once** and are
  identical for every engine, so the case states *"every engine agrees"* rather than enumerating
  what each one does. A variant fills in blanks; it cannot restructure.
- **⚠ A variant may not carry `COLUMNMAP` or `KEYCOLUMNS`, and the loader refuses one that tries.**
  Those change the *mapping*, and a different mapping writes different rows — which needs its own
  golden and therefore its own case. A variant free to change them could make any answer come out
  right, quietly destroying the single shared expectation the construct rests on. (Varying the
  table's *name* is fine and expected: engines spell identifiers differently.)
- **Each variant's DDL runs through the variant's own route**, so one engine's SQL can never reach
  another's connection. A case-level `ddl:` beside `variants:` is refused for the same reason.
- **It composes with `matrix:`.** Four variants × eight permutations is **32 executions of one
  authored case**.
- **Every failure names the run**: `[postgres, UseUpsert=true]`.

**What this tier does NOT prove.** The harness stubs `RetriableWriter` and the recovery family,
so a target case tests the writer against *our model* of the platform lifecycle — the same thing
its unit tests do. Proving platform integration is the live tier's job. What this tier adds is the **real
database**: a real driver, real metadata, real type conversion, in seconds rather than the minutes a
live case costs. The stubs' own javadoc names each reduction; `com.webaction.recovery.Stemma` and
`com.webaction.ser.KryoSingleton` carry the two that matter most (a one-level lattice, and bytes
that are not the platform's Kryo encoding).

## Reader ops: `IntegrationSeams`

The harness resolves four constructor parameter types on its own — `java.util.Map`, `Logger`,
`BuiltInFuncs`, `TypeResolver` — which covers every in-stream op. **Readers ask for
module-specific seams instead**, and those seams have no overlap with one another, so no shared
type can absorb them and a harness that special-cased each module would stop being op-agnostic.

The `Logger` is built from the case's `EnableLogging` and `LogSink` properties, as an App builds
it, so a case can turn debug on and choose a sink. The node-level logger configuration still
applies; a module's own log4j default does not, since the harness cannot see it.

An op that needs such a seam ships one class in its core package:

```java
package com.example.MyReaderOp;

public final class IntegrationSeams {

    /** Called once per constructor parameter the harness cannot resolve itself. */
    public static Object seamFor(String parameterTypeName, Map<String, Object> properties) {
        // Compare on Class.getName(), NOT a suffix -- a nested type arrives as Outer$Inner,
        // which endsWith(".MyTableReader") silently misses.
        if (MyTableReader.class.getName().equals(parameterTypeName)) {
            return new ScriptedTableReader(properties);   // built from the case's properties
        }
        if (MySourcePosition.class.getName().equals(parameterTypeName)) {
            return null;   // "pass null, deliberately" -- no recovered position on a first run
        }
        // An unrecognised type MUST throw. Returning null here would inject a silent null and
        // the case would NPE later, or assert "0 events" and pass for the wrong reason.
        throw new IllegalArgumentException("no seam for " + parameterTypeName);
    }
}
```

- **`null` means "pass null, deliberately", and nothing else.** A reader's recovered-position
  parameter is null on a first run, so the harness cannot treat null as an error — which is
  exactly why an unrecognised type has to throw rather than fall through to `return null`.
- **A primitive parameter is satisfied by its wrapper** (`int` ← `Integer`); returning `null` for
  one is rejected, since it would otherwise fail inside `newInstance` with "argument type
  mismatch".
- **The seam sees the same enriched properties the core does** — reserved
  `striim.op.namespace`/`striim.op.sourceName` keys included, `Password`-typed entries wrapped —
  as a defensive copy, so a seam that mutates cannot change what the core receives.
- **A wrong-typed return fails where it is produced**, naming both what came back and what was
  asked for — otherwise it surfaces as an opaque `newInstance` error.
- **Missing class, wrong signature, or a seam that throws** each fail with a message naming the
  convention rather than a bare reflection error.

It may live in `src/main`: it is inert in production — nothing calls it
unless a harness does — and the alternative is a constructor shape that exists only to be
testable, which the one-declared-constructor rule forbids.

For changes to harness class names, see [Upgrading the Java harness](UPGRADING.md).
