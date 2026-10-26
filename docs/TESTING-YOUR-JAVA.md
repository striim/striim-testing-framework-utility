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

Jar filenames normally end in the Striim series, such as `ReferenceOpV1-5.4.jar`. When the
`STRIIM_BUILD_<version>` build matrix holds several releases of one series, the jars a run builds
use the shortest unique version prefix instead: 5.4.2 and 5.4.4.0 use `-5.4.2.jar` and
`-5.4.4.jar`; 5.4.4.0 and 5.4.4.1 need all four parts. Only the matrix counts, not other installs
on the machine; without one, naming stays at the series. With several releases in the matrix,
each release builds in its own Maven output directory, so building or cleaning one keeps the
others' jars. The deployed operator name stays the same.

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

## Testing a prebuilt jar

To test the exact jar you ship, rather than one the run builds from your sources, point the run at
it. The case still names its module with `jar:`; the run uses your file instead of running
`mvn package`, in live and integration cases alike. Build the jar once per Striim series and test
that file on each release of the series.

| Variable | Selects |
|---|---|
| `SLT_PREBUILT_JAR_<MODULE>` | the jar for one module. `<MODULE>` is the module folder's name in upper case, with any other character as `_`: `SLT_PREBUILT_JAR_REFERENCEOP` for `ReferenceOp`. |
| `SLT_PREBUILT_JAR` | one jar for every module the run builds; use it when the run builds a single module. |
| `SLT_PREBUILT_DIR` | a folder holding the jars. |

The first one set wins, in that order. Set them in the shell; `.env` does not hold them.

- **A folder**, which any of the three may name, must hold exactly one jar for the module and the
  release's series, named `<module>[V<version>]-<series>.jar` (for example `ReferenceOpV1-5.4.jar`),
  in it or below it. A jar named with the release's longer prefix above
  (`ReferenceOpV1-5.4.2.jar`) is used before the series jar. No match, or more than one, is an
  error and the case does not run; another module's jar is never used.
- **A named file** must end in `-<series>.jar` for the release's series (for example
  `ReferenceOpV1-5.4.jar`), or in the longer prefix; otherwise the case does not run.
- **A shared named file** selected by `SLT_PREBUILT_JAR` is refused if its `Striim-Module-Name`
  manifest entry names another module. The entry must match the module folder's name, optionally
  followed by `V<version>`, ignoring case. A jar without the entry (every UDF jar) must instead be
  named for the module, as in a folder. These checks do not apply to `SLT_PREBUILT_JAR_<MODULE>` or
  `SLT_PREBUILT_DIR`; folder lookup already selects only this module's jar.
- **Before using it**, the run checks that the file is a readable jar and that its classes target
  the release's Java level (17 for Striim 5.4, 11 for 5.0 and 5.2). If not, the case does not run.
- **The run reports** the file and its SHA-256 (`using prebuilt jar …`) and loads those exact
  bytes; an Open Processor's upload name carries a tag of that digest.

```bash
export SLT_PREBUILT_JAR_REFERENCEOP=$PWD/dist/5.4/ReferenceOpV1-5.4.jar
striim-test run samples/code/op
```

A jar with no module of its own is loaded by the case instead: [TEST-YAML.md](TEST-YAML.md),
"`load:` — a prebuilt jar".
