# ReferenceUdf: a minimal UDF library

A Striim UDF library with one `WAEvent → WAEvent` function that copies its input and stamps
`userdata.processed=true` on the copy. It has no business logic on purpose: it shows the shape a
UDF function should have, so you can copy the module and replace the body with your transform.

The live test that loads it is `samples/code/udf` ([README](../../../README.md)); building and
testing your own module is covered in
[docs/TESTING-YOUR-JAVA.md](../../../../../../docs/TESTING-YOUR-JAVA.md).

## Metadata

- **Component type:** UDF library
- **Class:** `com.example.ReferenceUdf`
- **Jar:** `ReferenceUdfV1-5.4.jar`
- **Input → Output:** `WAEvent` → `WAEvent`
- **Built against:** the Striim release in `STRIIM_HOME` (the pom defaults to 5.4.0.6, Java 17)
- **Dependencies:** `com.webaction.proc.events.WAEvent` from the Striim Platform and Common jars
  (system scope); JUnit Jupiter, for tests only

## Functions

| Function | Scope | What it does |
|---|---|---|
| `ReferenceUdfMarkProcessed(in)` | one event | copies `in` with `WAEvents.copyEvent` and sets `userdata.processed = "true"` on the copy |
| `ReferenceUdfSetLogging(enabled)` | the whole server | turns this library's stdout tracing on or off at runtime and returns the value set |

```sql
CREATE OR REPLACE STREAM ProcessedStream OF Global.WAEvent;

CREATE OR REPLACE CQ MarkProcessedCq
INSERT INTO ProcessedStream
SELECT com.example.ReferenceUdf.ReferenceUdfMarkProcessed(s)
FROM SrcStream s;
```

Call a function by its full name; no `CREATE FUNCTION` is needed once the jar is loaded.
`ReferenceUdfSetLogging` returns its argument so it can sit inside a `SELECT`. It is process-wide:
one call changes every later call on that server.

## The shape every function follows

- **Null-safe.** A null input returns null, never a `NullPointerException`.
- **Never throws.** The body is wrapped in a try/catch; on an exception the function logs to stderr
  and returns the original input unchanged. Nothing in this body can throw: the wrapper is there so
  your transform starts with it.
- **Never mutates its input.** Only the returned copy carries the stamp.
- **`WAEvents.copyEvent`, not `WAEvent.makeCopy`.** The platform's `makeCopy` drops several
  `SimpleEvent` fields, among them the partition key; `copyEvent` (from the shared `SampleCommon`
  code compiled into the jar) restores them.
- **Deterministic, so not annotated `@Nondeterministic`.** The same input always gives the same
  output.

## Build

The live test builds the jar for you. By hand, pass the release in `STRIIM_HOME` to Maven:

```bash
export STRIIM_HOME=/path/to/striim
ver=$(basename "$STRIIM_HOME"/lib/Platform-*.jar .jar); ver=${ver#Platform-}
mvn package -DSTRIIM_VERSION=$ver -DSTRIIM_SERIES=${ver%.*} -DJAVA_RELEASE=17
```

The jar is `target/ReferenceUdfV1-5.4.jar`. Releases 5.0.x and 5.2.x need JDK 11.

## Examples

[`examples/`](examples/README.md) holds a fixture per function (`input.txt` → `expected.txt`),
checked by `ExamplesGuardTest` under `mvn test`, and a runnable `app.tql`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No `[ReferenceUdf] ... in : ... out: ...` lines on stdout | logging is off by default | call `ReferenceUdfSetLogging(true)` |
| The stamp is not in a database target | userdata is not a column | send the stream to a `FileWriter` with `JSONFormatter` as well, as the live test does |
