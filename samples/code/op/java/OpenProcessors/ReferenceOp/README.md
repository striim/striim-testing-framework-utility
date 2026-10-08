# ReferenceOp — Open Processor Adapter

The smallest complete Open Processor: it copies every inbound `WAEvent` through unchanged and
stamps one userdata key onto the copy. It exists to be read and copied: the shape of a transform,
with nothing in it that a transform does not need.

## Metadata

- **Component type:** Open Processor (`AdapterType.process`)
- **Implementation class:** `com.example.ReferenceOpV1.App`
- **Core class:** `com.example.ReferenceOpV1.Processor`
- **Module name:** `ReferenceOpV1` — `Striim-Module-Name: ReferenceOpV1`
- **Service interface:** `com.webaction.runtime.components.openprocessor.StriimOpenProcessor`
- **Built jar:** `ReferenceOpV1-5.4.jar`
- **Input → Output:** `com.webaction.proc.events.WAEvent` → `com.webaction.proc.events.WAEvent`
- **Built against:** Striim `5.4.0.6`, Java `17`
- **Shared layer:** `OpenProcessorCommon` and `SampleCommon`, add-sourced and shade-relocated into
  the module's own jar

## Overview

ReferenceOp performs one transform: for each event it receives, it produces a copy that is
indistinguishable from the source except for a single userdata entry, `processed=true`. The source
event is never mutated.

It sits anywhere a passthrough with a marker is useful — proving a pipeline is wired correctly,
tagging events that have traversed a particular flow, or serving as the starting point for a new
Open Processor.

Its second purpose is pedagogical. A pure copy needs no platform seam at all, yet this module
accepts a `BuiltInFuncs` seam through the three-argument core constructor, as a worked example of
that shape. The seam is gated behind `EnableInspection`, which is off by default;
with it off, the seam is never called.

## How It Works

1. **`App.start()`** — inherited from `AbstractOpenProcessorApp`. It builds the logger from the
   deployed component's namespace and name, reads `EnableLogging`, and enriches the property map
   with the reserved namespace and source-name keys.
2. **`App.buildProcessor(props)`** — constructs `Processor` with the property map, a
   `BuiltInFuncResolver` (the production `BuiltInFuncs` implementation) and the logger.
3. **`Processor(props, funcs, logger)`** — reads `EnableInspection` once, at construction.
4. **`Processor.processEvent(event)`** — per event:
   - a `null` event returns an empty list, emitting nothing;
   - otherwise `WAEvents.copyEvent` produces the copy, and `processed=true` is written to the
     copy's userdata;
   - when `EnableInspection` is on, each source column's presence is recorded onto the copy as
     `column <i>`;
   - the copy is returned as a single-element list.
5. **`Processor.close()`** — no-op. The component holds no resources.

**Why `WAEvents.copyEvent` and not `WAEvent.makeCopy`.** The platform's `makeCopy` carries
`data`/`before` and their presence bitmaps, `metadata`/`userdata`/`aiData`, the timestamp and the
type UUIDs — and silently drops every other `SimpleEvent` field. The costly one is `key`:
`SimpleEvent` implements `Partitionable`, so a raw `makeCopy` loses the partition assignment with
no exception and no log line. `WAEvents.copyEvent` re-stamps the dropped fields.

## Parameters

| Parameter | Label | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `EnableLogging` | Enable Logging | Boolean | No | `false` | Enable Logging |
| `EnableInspection` | Enable Inspection | Boolean | No | `false` | Record on each emitted event whether the source supplied each column, as one `column N` userdata entry per column |

**Note on `EnableInspection`.** The result lands on the emitted event, not in the log, so it is
observable downstream without `EnableLogging` being on.

**Note on both parameters.** Each is declared `Boolean.class` but read with
`Boolean.parseBoolean` over the property's string form, so any value other than a case-insensitive
`true` is false.

## Input

`com.webaction.proc.events.WAEvent`.

The core reads `data` (as an array, positionally, and by reference identity when inspection is on)
and passes the whole event to `WAEvents.copyEvent`. It reads no metadata or userdata key by name.

## Output

`com.webaction.proc.events.WAEvent` — exactly one event per non-null input event, on the stream the
`OPEN PROCESSOR` declares with `INSERT INTO`.

**Per-situation shapes:**

| Situation | Emitted |
|---|---|
| Ordinary event | One copy. `data`, `before`, both presence bitmaps, `metadata` and the type identity match the source. `userdata.processed = "true"` is added; any userdata the source carried is preserved alongside it. |
| Ordinary event, `EnableInspection: 'true'` | As above, plus one `column <i>` key per source column, value `Boolean.TRUE` or `Boolean.FALSE`. |
| `null` event | Nothing. An empty list is returned. |

A concrete success event, for a two-column source row carrying `userdata.origin=sourceA`:

```
data:     ["a", "b"]                 (unchanged)
before:   as source                  (unchanged)
metadata: as source                  (unchanged)
userdata: { "origin": "sourceA", "processed": "true" }
```

The same event with `EnableInspection: 'true'`:

```
userdata: { "origin": "sourceA", "processed": "true",
            "column 0": true, "column 1": true }
```

The core raises no exception of its own, but the component does have an error shape: on a failure
anywhere in processing or sending, the **original, unstamped** event is forwarded instead of the
copy. See **Error Handling & Retries**.

## Quickstart — build

    export STRIIM_HOME=/path/to/Striim       # your local install (any release)
    ver=$(basename "$STRIIM_HOME"/lib/Platform-*.jar .jar); ver=${ver#Platform-}
    ( cd samples/code/op/java/OpenProcessors/ReferenceOp && \
      mvn package -DSTRIIM_VERSION=$ver -DSTRIIM_SERIES=${ver%.*} -DJAVA_RELEASE=17 )

**The installed version is the version.** The version comes from `STRIIM_HOME`; you build the
jar against that exact version and the live harness tests that same version. To switch versions,
point `STRIIM_HOME` at a different install and rebuild (see `docs/TESTING-YOUR-JAVA.md`, "Building by
hand").

Building against a 5.0.x or 5.2.x release requires JDK 11; 5.4.x builds on JDK 17.

An editor launched from the Dock or Finder does not inherit shell environment, so `STRIIM_HOME`
must be exported in the shell that runs the build.

## Build & Deploy

1. Build as above. The shaded jar lands at `target/ReferenceOpV1-5.4.jar`.
2. Copy it into the server's `UploadedFiles/`.
3. Load it once per cluster:

   ```sql
   LOAD OPEN PROCESSOR 'UploadedFiles/ReferenceOpV1-5.4.jar';
   ```

   The statement takes the jar path and nothing else — there is no module-name argument.

4. Reference it from TQL as `Global.ReferenceOpV1`.

Replacing a loaded jar requires `UNLOAD OPEN PROCESSOR 'UploadedFiles/ReferenceOpV1-5.4.jar';`
followed by the same `LOAD`. The build is
reproducible — an unchanged tree produces a byte-identical jar — so a rebuild alone is not a
reason to reload.

## Usage Example (TQL)

```sql
CREATE OR REPLACE STREAM ProcessedStream OF Global.WAEvent;

CREATE OR REPLACE OPEN PROCESSOR ItemsCopy USING Global.ReferenceOpV1 (
  EnableLogging:     'true',
  EnableInspection:  'false'
) INSERT INTO ProcessedStream
FROM ItemsSourceStream;
```

The runnable form, with a source and two targets, is in `examples/copy-adds-userdata/`.

## Error Handling & Retries

The core has no retry path and raises no exception of its own. A `null` event is absorbed and
produces no output rather than failing the batch.

**The inherited error model is best-effort passthrough, and it has an observable output shape.**
`ReferenceOp` does not override `failBatchOnEventError`, so the default applies: if `processEvent`
or the downstream send throws, `AbstractOpenProcessorApp.run()` logs the failure and then forwards
the **original** event — the one that was never copied and never stamped. Downstream therefore
receives an event with **no `userdata.processed`**, and nothing is retried. If that second send
also fails, the event is dropped and only a log line records it.

An event arriving downstream without the marker is that path, and the server log is where the
cause is.

`WAEvents.copyEvent` re-stamps the fields `makeCopy` drops using reflection, and that re-stamping
is **best-effort**: `copyField` catches `Throwable` and continues. If a field cannot be set — an
inaccessible field under a stricter JDK, or a `WAEvent` stand-in that does not declare it — the
copy is emitted without it, with no exception and no log line. `key` is among those fields, so this
is the same partition-loss hazard described above, arriving by a different route.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Events arrive downstream without `userdata.processed` | The stream is bypassing the processor — check that the target's `INPUT FROM` names the processor's output stream, not the source's. |
| `column <i>` keys absent with `EnableInspection: 'true'` | The property is read once at construction. Changing it requires redeploying the application. |
| Every `column <i>` reports `false` | The source event's presence bitmap is present but zeroed. `IS_PRESENT` reads the bitmap, not the value, so a source that never set it reports every column absent. |
| With `EnableInspection: 'true'`, events arrive with **no** `userdata.processed` | The source event has `data` but a **null** `dataPresenceBitMap`. `IS_PRESENT` dereferences the bitmap without a null check, so it throws; inspection fails, the inherited error model forwards the original event, and the marker never gets written. Turn inspection off for such a source. |
| The operator loads but TQL cannot find it | `Global.ReferenceOpV1` must match the `Striim-Module-Name` in the jar manifest. |

## Notes / Limitations

- **The `BuiltInFuncs` seam is not a requirement for a transform.** It is present here to
  demonstrate the three-argument core constructor. A transform that needs no platform seam should
  use the two-argument form, `Processor(props, logger)`.
- **`IS_PRESENT` is a presence check, not a null check.** A column explicitly set to `NULL` is
  present. "Not present" means the source supplied no value for that column — the ordinary shape of
  a CDC UPDATE image carrying only changed columns.
- **`IS_PRESENT` selects its bitmap by reference identity.** It compares the image argument against
  `event.data` and `event.before` with `==`; any other array matches neither and reports every
  column absent, with no error.
- **Inspection cost scales with column count** — one userdata entry per column per event. It is off
  by default for that reason.
- The component declares no batching, and emits per event.
