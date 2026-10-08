# UDF sample: `referenceudf-mark-processed`

A Striim UDF library with one function, `ReferenceUdfMarkProcessed`. It takes a `WAEvent`,
returns an unchanged copy, and sets `userdata.processed=true` on the copy. The app calls it
from a CQ between a Postgres `DatabaseReader` and two targets.

## Layout

| Path | What it is |
|---|---|
| `referenceudf-mark-processed/test.yaml` | the case |
| `referenceudf-mark-processed/expected/out.csv` | rows the Postgres target must hold |
| `referenceudf-mark-processed/expected/userdata.csv` | the `processed` stamp the file target must show |
| `java/UserDefinedFunctions/ReferenceUdf/` | the Maven module that `udf.jar:` names |
| `java/UserDefinedFunctions/ReferenceUdf/examples/ReferenceUdfMarkProcessed/` | the directory that `example:` names: `app.tql`, `ddl.sql`, `seed.sql` |
| `java/SampleCommon/src/main/java/` | shared helper code (`WAEvents`), compiled into the UDF jar by the pom |
| `java/UserDefinedFunctions/UDF-reference-pom.xml` | a template pom for starting a new UDF module (not built) |

## What the case checks

- `smoke`: the app reaches RUNNING.
- `data`: the target table matches `expected/out.csv`, so the columns passed through unchanged.
- `file`: at least 1 JSON event the `FileWriter` wrote carries `userdata.processed`, matching
  `expected/userdata.csv`. A database target can't show userdata, so this check needs the file.

## Build and run

```bash
export STRIIM_HOME=/path/to/striim     # holds lib/Platform-*.jar
striim-test run samples/code/udf    # from the clone root; builds the jar first
```

To build the jar by hand, see "Building by hand" in
[docs/TESTING-YOUR-JAVA.md](../../../docs/TESTING-YOUR-JAVA.md): on any release other than 5.4.0.6,
Maven needs the release passed in.

To use this as the start of your own UDF, copy `java/` into your repo and change the module name
and function. Then point `udf.jar:` at the new module, by its path from your repo's root
([docs/TESTING-YOUR-JAVA.md](../../../docs/TESTING-YOUR-JAVA.md), "How a case points at its code").
