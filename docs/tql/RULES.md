# TQL design and performance rules

The rules a TQL application should follow, each with the reason and how to check it. A review cites
them by ID ([REVIEW.md](REVIEW.md)). Syntax is in [REFERENCE.md](REFERENCE.md). A rule can be broken
when the requirement needs it; say which rule and why in a comment next to the component.

## Structure

**S1. Define each component with `CREATE OR REPLACE`.** Re-running the file then replaces it instead
of failing with "already exists".

**S2. Set each property once.** When a key repeats, the last value wins without a warning, and a
reader cannot tell which one was meant. Remove properties that only restate the default, and
export leftovers such as `adapterName` or `connectionProfileName: ''`.

**S3. Every stream has a producer and at least one consumer.** A stream nothing reads is either a
lost branch or dead code. Trace each stream from its `OUTPUT TO`/`INSERT INTO`/`ROUTE TO` to its
`FROM`/`INPUT FROM`.

**S4. No secrets in the file.** Passwords and keys come from a vault (`'[[vault.key]]'`) or a
property variable (`'$name'`). Hosts and environment-specific names also belong in property
variables when the same TQL is deployed to more than one environment.

**S5. DatabaseReader: `Tables` or `Query`, never both.** `Query` wins silently and `Tables` then only
names the events. If both are set, flag it.

**S6. DatabaseReader reads tables in the order `Tables` lists them.** List parent tables before
child tables, so children do not arrive before the rows they reference.

## CQs

**C1. Combine a chain of CQs into one.** Each CQ adds a stream hop. Two CQs where the second only
reads the first's output can usually be one CQ by nesting the expressions:

```sql
-- two CQs
CREATE OR REPLACE CQ Tag INSERT INTO Tagged SELECT putUserData(e, 'region', 'EU') FROM Raw e;
CREATE OR REPLACE CQ AddKey INSERT INTO Keyed SELECT com.example.Ids.withKey(e, 'ACCOUNT_ID') FROM Tagged e;

-- one CQ
CREATE OR REPLACE CQ TagAndKey INSERT INTO Keyed
SELECT com.example.Ids.withKey(putUserData(e, 'region', 'EU'), 'ACCOUNT_ID') FROM Raw e;
```

A long chain of `CASE WHEN <variant> THEN <transform> ELSE e END` CQs, each passing every event
through, collapses into one CQ with one `CASE`, or a router when the branches go to different
targets.

**C2. Read columns by position with `data[n]`.** `GETDATA(e, 'COL')` looks the name up for every
event. Use it only when one CQ handles several tables in which the column sits at different
positions. Comment what each index is: `TO_LONG(e.data[0]) -- ORDER_ID`.

**C3. Every `CASE` has an `ELSE`.** A `CASE` with no match and no `ELSE` fails at run time.

**C4. Do not join a stream to a cache with an inner join unless a miss should drop the event.**
Use `LEFT JOIN` and handle the null.

**C5. Use `TO_DATEF` for date parsing in a hot path.** It is much faster than `TO_DATE`.

**C6. Build JSON in a JSON Open Processor or UDF, not in nested CQ string expressions.** Nested
`'{"a":' + ... + '}'` concatenation is slow, unescaped and hard to read.

## User data and mapping

**M1. `putUserData` takes key/value pairs; set several in one call.**
`putUserData(e, 'k1', v1, 'k2', v2)`, not `putUserData(putUserData(e, 'k1', v1), 'k2', v2)`.

**M2. Put a value into user data only when it is new or changed.** Copying an unchanged column,
`putUserData(e, 'STATUS', GETDATA(e, 'STATUS'))`, costs a copy of the event and does nothing a
`ColumnMap` entry could not. Map the source column on the target instead.

**M3. `ColumnMap` lists only the columns whose names differ.** One entry switches the writer to
name matching for every other column. If every name matches, keep one entry such as
`ColumnMap(ID=ID)` so the table is matched by name, not by position.

**M4. Mapping comes from metadata and user data with `@METADATA(key)` and `@USERDATA(key)`.** Do
not copy metadata into user data or into the row just to map it.

## Ordering and parallelism

**P1. One path per table unless parallelism is required.** Splitting a stream into several CQs or
targets that run side by side loses ordering between them. Check that there is no parallel
processing the requirement did not ask for.

**P2. Tables with foreign keys between them share a stream and a writer.** A parent and its child
on separate writers commit independently, so a child row can reach the target before its parent.
Do not fix this with a delay window.

**P3. A router is for separate processing, not for speed by default.** Route when tables need
different CQs or targets. When a router is used for throughput, keep each table, and each
parent/child group, on one branch (P1, P2).

**P4. Writer `ParallelThreads` is for initial load only.** Recovery turns it off, and it does not
keep order.

## Lookups

**L1. Use one lookup Open Processor for database lookups in a high-throughput app.** It pools
connections and caches results with expiry. `CACHE` holds the whole table in memory and refreshes
all of it; `EXTERNAL CACHE` queries the database on every lookup. One lookup processor per
application is usually enough; give it several lookups in its configuration rather than adding
processors.

**L2. A `CACHE` joins on its `keytomap` field.** Joins on any other field scan the cache.

## Writers

**W1. Spanner: at most 2,000 rows per batch.** A Spanner commit is limited to 80,000 mutations
([Spanner quotas](https://cloud.google.com/spanner/quotas)), and each row writes one mutation per
column and index. Set `BatchPolicy: 'EventCount: 2000, Interval: <t>'` with `t` about the time the
app takes to receive 2,000 events, so batches fill before the interval ends.

**W2. Spanner: write JSON columns from a ready JSON value.** On 5.4.0.6 and later, a `ColumnMap`
entry that builds JSON in the writer (`JSON_OBJECT(...)`, a dotted sub-path) makes SpannerWriter
use SQL statements instead of mutations, which is many times slower. Build the JSON upstream and
map it as a plain column.

**W3. DatabaseWriter: `CommitPolicy` is at least `BatchPolicy`.**

**W4. Each target column is mapped once.** Two dotted sub-path entries for one JSON column in a
single `ColumnMap` keep only the last.

**W5. `PreserveSourceTransactionBoundary: true` needs the reader to pass transaction boundaries.**
CDC readers drop them by default (`FilterTransactionBoundaries: true`); a writer waiting for a
boundary that never comes never commits. Set `FilterTransactionBoundaries: false` on the reader
when the writer preserves boundaries. On SpannerWriter, `IgnorableExceptionCode` has no effect
while boundaries are preserved.

## Recovery and errors

**R1. Recovery interval: 1 to 3 minutes** (`RECOVERY 1 MINUTE INTERVAL` to `RECOVERY 3 MINUTE
INTERVAL`). Shorter intervals checkpoint more often for little gain; longer ones replay more after
a restart.

**R2. Ignorable exception codes are temporary in CDC.** `DUPLICATE_ROW_EXISTS`, `NO_OP_UPDATE` and
`NO_OP_DELETE` are expected only while CDC catches up after an initial load. Remove them once it
has; left on, they hide real data errors.

**R3. Decide the exception policy explicitly.** List the exceptions in `EXCEPTIONHANDLER` and add
`USE EXCEPTIONSTORE` when any is `IGNORE`, so dropped events can be found.

**R4. `QUIESCE`, not `STOP`, before a planned change.** `QUIESCE` flushes what is in flight;
`STOP` does not.

**R5. To change a deployed, recoverable app, quiesce it and use `ALTER`, not `CREATE OR REPLACE`.**
Replacing a flow can interfere with recovery. To change a recoverable DatabaseReader's `Tables`,
export the app, drop it, edit the TQL and import it.

## Delivering TQL

These apply to an assistant writing or changing TQL:

**D1. Deliver the whole file**, never a fragment or `...`.

**D2. Say what changed and why**, citing these rule IDs.

**D3. Include every file the TQL reads**: Open Processor configuration JSON, mapping files, CSVs.

**D4. Trace every stream path** (S3) before delivering, and say where ordering is and is not kept.
