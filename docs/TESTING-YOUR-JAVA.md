# Testing your own Java: Open Processors and UDFs

A live test can build your Maven module, load the jar into Striim, and then deploy the app that
uses it. The test proves the module works inside a running app. To test the code's behaviour on its
own, event in and event out without a Striim server, use the integration tier
([INTEGRATION-TESTS.md](INTEGRATION-TESTS.md)); most modules want both.

## What you need

- Maven and a JDK 17 on `PATH` (a JDK 11 for Striim 5.0 and 5.2).
- `STRIIM_HOME`, a Striim install: the module compiles against the jars in its `lib/`, for the
  release it finds there.
- **Against your own Striim server** (`STRIIM_URL` set), `STRIIM_HOME` must be that running
  server's own install, on the machine you run `striim-test` on. The run copies each jar into
  `$STRIIM_HOME/UploadedFiles`, where the server loads it from. Another install of the same release
  does not work (the case fails with `native-uploads-dir-missing`), and neither does a server on
  another host. In Docker mode, any install of the release will do.

`striim-test doctor --case <case>` checks `STRIIM_HOME` for you.

## Start from a sample

| Sample | Loads | Case |
|---|---|---|
| [`samples/code/op`](../samples/code/op/README.md) | an Open Processor, `ReferenceOpV1` | `referenceop-copy-adds-userdata` |
| [`samples/code/udf`](../samples/code/udf/README.md) | a UDF library, `ReferenceUdf` | `referenceudf-mark-processed` |

Both copy Postgres rows from a source table to a target table and stamp `userdata.processed=true`
on every event. Run them from the clone root:

```
striim-test run samples/code        # or samples/code/udf, samples/code/op
```

To start your own module, copy the sample's `java/` folder into your repo, rename the module, and
change the core class (`Processor.java` for the OP, the function for the UDF). Each module's own
README describes its shape: [ReferenceOp](../samples/code/op/java/OpenProcessors/ReferenceOp/README.md),
[ReferenceUdf](../samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/README.md).

## How a case points at its code

<!-- snippet: fragment -->
```yaml
example: samples/code/udf/java/UserDefinedFunctions/ReferenceUdf/examples/ReferenceUdfMarkProcessed
tql: app.tql
udf:
  jar: samples/code/udf/java/UserDefinedFunctions/ReferenceUdf
```

- **`op:` or `udf:`** says how the jar is loaded: `op:` runs `LOAD OPEN PROCESSOR`, `udf:` loads a
  UDF library. Both may appear, and each takes a list for several modules.
- **`jar:` is the Maven module folder**, not a jar file. It must sit under a
  `java/OpenProcessors/` folder for `op:`, or `java/UserDefinedFunctions/` for `udf:`. Before the
  case runs, the framework runs `mvn package` there when the jar is missing or older than its
  sources.
- **`example:`** is the folder holding the app's TQL, DDL and seed files. Only `expected/` stays
  next to `test.yaml`. Leave it out to keep everything in the test folder.
- **Both paths are relative to the project root**: `SLT_PROJECT_ROOT`, or the framework clone when
  it is not set. A test in your own repo names its modules from your repo's root (for example
  `jar: java/OpenProcessors/MyOp`), so the module lives in your repo, next to the tests.
- **Tokens:** the OP sample's TQL names the processor `Global.${OP_NAME}`, the module's name
  (`MyOp`); `${OP_JAR}` is the file uploaded and loaded, whose name carries a tag of its contents
  (`MyOp-<tag>-5.4.jar`). The UDF sample calls its functions by their full names. With
  several modules, give each a `token:` (`token: MAP` gives `${MAP_NAME}`, `${MAP_JAR}`).

[TEST-YAML.md](TEST-YAML.md), "`op:` / `udf:`", has every key.

## Showing what your code did

A database target stores the columns, not the userdata an OP or UDF adds. That is why both samples
send the same stream to two targets: a Postgres table, compared with `data`, proves the columns
passed through unchanged; a `FileWriter` with `JSONFormatter`, checked with `file`, shows the
userdata. Choose the target that can show the effect you are testing.

## Building by hand

The run builds each jar for you. To build one yourself, pass the release to Maven: the sample poms
default to 5.4.0.6, so a plain `mvn package` fails on any other install with "could not find
artifact … Platform-5.4.0.6.jar".

```bash
export STRIIM_HOME=/path/to/striim
ver=$(basename "$STRIIM_HOME"/lib/Platform-*.jar .jar); ver=${ver#Platform-}
cd samples/code/udf/java/UserDefinedFunctions/ReferenceUdf
mvn package -DSTRIIM_VERSION=$ver -DSTRIIM_SERIES=${ver%.*} -DJAVA_RELEASE=17
```

An editor started from a desktop launcher does not inherit your shell's environment, so export
`STRIIM_HOME` in the shell that runs the build.
