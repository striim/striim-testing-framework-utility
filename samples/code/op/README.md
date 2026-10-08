# Open Processor sample: `referenceop-copy-adds-userdata`

An Open Processor, `ReferenceOpV1`, that copies every event through unchanged and sets
`userdata.processed=true` on the copy. The app puts it between a Postgres `DatabaseReader` and
two targets.

## Layout

| Path | What it is |
|---|---|
| `referenceop-copy-adds-userdata/test.yaml` | the case |
| `referenceop-copy-adds-userdata/expected/items.csv` | rows the Postgres target must hold |
| `referenceop-copy-adds-userdata/expected/userdata.csv` | the `processed` stamp the file target must show |
| `java/OpenProcessors/ReferenceOp/` | the Maven module that `op.jar:` names |
| `java/OpenProcessors/ReferenceOp/examples/copy-adds-userdata/` | the directory that `example:` names: `app.tql`, the DDL and seed files, and a README walking through the pipeline |
| `java/OpenProcessors/OpenProcessorCommon/src/main/java/` | the shared OP base classes (`AbstractOpenProcessorApp`, `EventProcessor`, `BuiltInFuncs`, `Logger` …), compiled into the OP jar by the pom |
| `java/SampleCommon/src/main/java/` | shared helper code (`WAEvents`), also compiled in |
| `java/OpenProcessors/OP-reference-pom.xml` | a template pom for starting a new OP module (not built) |

The pom relocates the shared `com.example.common` package to a module-specific name,
because Striim refuses to load two jars that declare the same class.

## What the case checks

- `smoke`: the app reaches RUNNING.
- `data`: the target table matches `expected/items.csv`, so the columns passed through unchanged.
- `file`: at least 2 JSON events carry `userdata.processed`, matching `expected/userdata.csv`.

## Build and run

```bash
export STRIIM_HOME=/path/to/striim     # holds lib/Platform-*.jar
striim-test run samples/code/op    # from the clone root; builds the jar first
```

To build the jar by hand, see "Building by hand" in
[docs/TESTING-YOUR-JAVA.md](../../../docs/TESTING-YOUR-JAVA.md): on any release other than 5.4.0.6,
Maven needs the release passed in.

To start your own OP, copy `java/` into your repo, rename the module and change `Processor.java`.
Then point `op.jar:` at the new module, by its path from your repo's root
([docs/TESTING-YOUR-JAVA.md](../../../docs/TESTING-YOUR-JAVA.md), "How a case points at its code").
