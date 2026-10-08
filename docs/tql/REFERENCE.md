# TQL syntax reference

The TQL you need to write and read a Striim application: statements, components, CQ expressions,
the WAEvent functions, and writer table mapping. It is written for Striim 5.4; items that differ
between 5.4 releases say so. For a component's full property list, see that adapter's page on
[docs.striim.com](https://www.striim.com/docs/platform/en/). What to do with this syntax is in
[RULES.md](RULES.md).

## Statements and text

- Every statement ends with `;`.
- Comments: `-- to end of line` and `/* ... */`.
- Keywords are case-insensitive. Object names may contain letters, digits and `_`, may not start
  with a digit, and must be unique in the namespace. Create a component before anything refers to it.
- There is no identifier quoting. Backticks are an error, and `"..."` is a string, not a name.
- `'...'` and `"..."` are the same kind of string. Backslash escapes are Java's (`\n`, `\t`, `\'`,
  `\\`, `\uXXXX`); any other backslash sequence fails to compile, so `'\d+'` does not compile and
  `'C:\temp'` holds a tab. Write regexes and Windows paths as `'\\d+'`, or use a raw string
  `'''\d+'''`, which has no escape processing.
- `''` inside a string is not an escaped quote.
- Keywords cannot be used as names, aliases, type fields or unquoted property keys. The ones that
  catch people: `TYPE`, `KEY`, `MAP`, `FORMAT`, `SCHEMA`, `SOURCE`, `TARGET`, `EVENT`, `USER`,
  `ROLE`, `ORDER`, `GROUP`, `LIMIT`, `OFFSET`, `INTERVAL`, `TIMEOUT`, `RANGE`, `ROW`, `ROWS`,
  `SESSION`, `PROPERTIES`, `ENRICH`, `END`, `ALL`, `ONE`, `NEW`, `CAST`, `OF`, and time units such
  as `DAY` and `SECOND`. The full list is under "Reserved keywords" in the
  [TQL reference](https://www.striim.com/docs/platform/en/tql-reference.html). A property key that
  is a keyword can be quoted: `'Format': 'x'`.
- The keyword rule also applies to Java method names called from a CQ: `java.util.List.of(1)` and
  `HexFormat.of()` do not parse, because `of` is a keyword.

## Applications and flows

```sql
CREATE OR REPLACE APPLICATION OrdersCdc
  RECOVERY 2 MINUTE INTERVAL
  EXCEPTIONHANDLER (AdapterException: 'STOP', InvalidDataException: 'STOP')
  USE EXCEPTIONSTORE TTL: '7d'
  AUTORESUME MAXRETRIES 3 RETRYINTERVAL 60;

-- components

END APPLICATION OrdersCdc;
```

| Clause | Syntax and behaviour |
|---|---|
| order | `WITH ENCRYPTION`, `RECOVERY`, `EXCEPTIONHANDLER`, `USE EXCEPTIONSTORE`, `AUTORESUME`, in that order |
| `RECOVERY n SECOND\|MINUTE\|HOUR INTERVAL` | checkpoint interval; singular unit only (`SECONDS` does not parse). Recovery can only be turned on when the app is created |
| `EXCEPTIONHANDLER (Name: 'STOP'\|'IGNORE', ...)` | names: `AdapterException`, `ArithmeticException`, `ClassCastException`, `ConnectionException`, `InvalidDataException`, `NullPointerException`, `NumberFormatException`, `SystemException`, `UnExpectedDDLException`, `UnknownException`. Database and driver errors from a source or target count as `AdapterException`. `IGNORE` drops the event and keeps running |
| `USE EXCEPTIONSTORE [TTL: '7d']` | keeps ignored and failed events in `<app>_ExceptionStore`; TTL default `7d`, units `s m h d w` |
| `AUTORESUME [MAXRETRIES n] [RETRYINTERVAL s]` | restarts after a halt or crash; bare `AUTORESUME` is 2 retries, 60 s apart |

`CREATE OR REPLACE APPLICATION` on an existing app drops it and every component in it first.

A flow groups components so they can be deployed separately, for example a source on a Forwarding
Agent and the rest on the cluster. Flows go inside an application:

```sql
CREATE OR REPLACE APPLICATION OrdersCdc;
CREATE FLOW SourceFlow;
CREATE OR REPLACE SOURCE OrdersSrc USING Global.PostgreSQLReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  ReplicationSlotName: 'striim_slot',
  Tables: 'public.orders'
) OUTPUT TO OrderChanges;
END FLOW SourceFlow;
END APPLICATION OrdersCdc;

DEPLOY APPLICATION OrdersCdc ON ALL IN default WITH SourceFlow ON ONE IN agents;
```

## Lifecycle

| Statement | Note |
|---|---|
| `DEPLOY APPLICATION a [ON ONE\|ALL IN group] [WITH flow ON ONE\|ALL IN group, ...];` | default `ON ONE IN default`; each `WITH` flow needs its `IN group` |
| `START [APPLICATION] a;` | |
| `QUIESCE [APPLICATION] a;` | pauses sources, flushes everything in flight, records recovery, stops. Use it, not `STOP`, before a planned change |
| `STOP [APPLICATION] a;` | stops; data in flight is not flushed |
| `RESUME [APPLICATION] a;` | from `HALTED` or `TERMINATED` |
| `UNDEPLOY APPLICATION a;` | `APPLICATION` is required here |
| `DROP APPLICATION a CASCADE;` | undeploy first; `CASCADE` drops the components too |

## Streams and types

`OUTPUT TO` and `INSERT INTO` create their stream if it does not exist, so most streams need no
`CREATE STREAM`. Declare one when its type must be fixed before the producer is compiled, for
example a stream of WAEvent that a CQ writes with `SELECT *`:

```sql
CREATE TYPE OrderType (orderId java.lang.Long KEY, status java.lang.String);
CREATE STREAM OrderEvents OF OrderType;
CREATE STREAM CleanChanges OF Global.WAEvent;
```

What a CQ's output stream carries:

- `SELECT e FROM s e`, or one function that returns the event (`putUserData(e, ...)`): the event
  itself, so a WAEvent stays a WAEvent with its metadata, user data and before image.
- Anything else (`SELECT TO_STRING(e.data[0]) AS id, ...`): a new typed event with one field per
  alias. Readers' metadata does not travel with it, and writers then map by position or field name.

## Sources

```sql
CREATE OR REPLACE SOURCE OrdersInitial USING Global.DatabaseReader (
  ConnectionURL: 'jdbc:postgresql://pghost:5432/shop',
  Username: 'striim',
  Password: '[[shopvault.pgpass]]',
  Tables: 'public.customers;public.orders',
  FetchSize: 1000
) OUTPUT TO InitialRows;
```

- One source can have several `OUTPUT TO` clauses, each with an optional `SELECT ... WHERE`.
- DatabaseReader:
  - If both `Query` and `Tables` are set, `Query` wins silently. Set one.
  - Tables are read one at a time in the order listed; a wildcard reads in the order the database
    returns. List parents before children.
  - `FetchSize` defaults to 100. `QuiesceOnILCompletion: true` quiesces the app when the load ends,
    when every target supports it.
- Reader wildcards: `public.%`, `public.ord%`.

## Continuous queries

```text
CREATE OR REPLACE CQ <name>
INSERT INTO <stream>
SELECT [DISTINCT] <expr> [AS <field>], ...
FROM <stream|window|cache> <alias> [, <window|cache> <alias>] [ [LEFT|RIGHT|FULL] JOIN ... ON <cond> ]
[WHERE <cond>] [GROUP BY <expr>] [HAVING <cond>] [ORDER BY <expr>] [LIMIT n]
[MODIFY (data[n] = <expr>, ...)];
```

- `CREATE CQ ... SELECT` without `INSERT INTO` is an ad-hoc query, not a CQ.
- A join needs bounded data on one side: a window, cache, event table or WActionStore. Two plain
  streams cannot be joined.
- An inner join with a cache drops the event when the cache has no matching key. Use `LEFT JOIN`
  to keep it.
- `CASE WHEN ... THEN ... END` with no `ELSE` fails at run time when nothing matches. Always write
  an `ELSE`.
- `MODIFY` changes WAEvent columns in place and keeps the event a WAEvent. Select only the alias,
  and assign only `data[n]` or `before[n]`:

```sql
CREATE STREAM TrimmedOrders OF Global.WAEvent;
CREATE OR REPLACE CQ TrimStatus
INSERT INTO TrimmedOrders
SELECT o FROM OrderChanges o
MODIFY (data[2] = TO_STRING(o.data[2]).trim());
```

- A UDF or any public static Java method can be called by its fully qualified name, or after
  `IMPORT STATIC com.example.Ids.*;`.

## WAEvent functions

Database readers emit `WAEvent`: `data` (the row after the change), `before` (the row before an
update, when the source supplies it), `metadata` (from the reader) and `userdata` (yours).

| Expression | Returns |
|---|---|
| `e.data[n]`, `e.before[n]` | column `n`, counting from 0, as `Object`; wrap in `TO_STRING`, `TO_LONG`, ... |
| `GETDATA(e, 'COL')`, `GETBEFORE(e, 'COL')` | the column by name, case-insensitive |
| `DATA(e)`, `BEFORE(e)` | the row as a map of column name to value |
| `IS_PRESENT(e, e.data, n)` | whether column `n` was supplied (false for columns a delete or partial update left out) |
| `META(e, 'Key')` | a metadata value; key is case-sensitive; a missing key returns `''`, not null |
| `USERDATA(e, 'key')` | a user-data value, or null |
| `putUserData(e, 'k1', v1, 'k2', v2, ...)` | a copy of the event with those keys set; nestable |
| `removeUserData(e, 'k', ...)`, `clearUserData(e)` | a copy without those keys |
| `replaceData(e, 'COL', v)` | changes the column in place; `v` must be the column's Java type |
| `replaceString(e, 'find', 'new')`, `replaceStringRegex(e, 'regex', 'new')` | string replacement across the row |
| `ChangeOperationToInsert(e)` | a copy whose operation is INSERT |
| `NVL(a, b)` | `b` when `a` is null. There is no `IFNULL` or `COALESCE` |
| `TO_DATEF(v, 'pattern')` | a date; much faster than `TO_DATE` |

Metadata keys every SQL CDC reader sets: `OperationName` (`INSERT`, `UPDATE`, `DELETE`),
`TableName`, `TxnID`, `TimeStamp`. Most also set `PK_UPDATE`; DatabaseReader sets `OperationName`
to `SELECT`. The rest are per reader; see the reader's "programmer's reference" page. Compare
metadata as strings: `TO_STRING(META(e, 'TableName')) = 'PUBLIC.ORDERS'`.

What a DELETE carries depends on the source: some send the key columns only, others the whole row
when the database logs it. Do not read non-key columns of a DELETE without checking.

## Routers

```sql
CREATE OR REPLACE ROUTER OrdersByTable INPUT FROM OrderChanges AS e CASE
  WHEN TO_STRING(META(e, 'TableName')) = 'public.orders' THEN ROUTE TO OrdersOnly,
  WHEN TO_STRING(META(e, 'TableName')) = 'public.order_lines' THEN ROUTE TO LinesOnly,
  ELSE ROUTE TO OtherTables;
```

- Commas between clauses, including before `ELSE`; no `END`.
- Every `WHEN` that is true gets the event. `ELSE` gets it only if none was true. Without `ELSE`,
  unmatched events are dropped.
- Each output stream may appear in one clause only, and cannot be the input stream. To send one
  condition to two streams, write the condition twice.

## Windows and caches

```sql
CREATE JUMPING WINDOW OrderBatches OVER OrderEvents KEEP 1000 ROWS WITHIN 10 SECOND;
CREATE WINDOW LastPerCustomer OVER OrderEvents KEEP 1 ROWS PARTITION BY orderId;
```

- `JUMPING` emits and empties the window each time it fills; without it the window slides.
- A count-only window (`KEEP n ROWS`) holds a partial batch until `n` more rows arrive. Add
  `WITHIN t` so the tail is released.
- Windows on system time are not recoverable; use `KEEP WITHIN t ON <timestamp field>` in a
  recoverable app.

```sql
CREATE TYPE RegionType (code java.lang.String KEY, name java.lang.String);
CREATE CACHE Regions USING Global.FileReader (directory: 'UploadedFiles', wildcard: 'regions.csv')
  PARSE USING Global.DSVParser (header: true)
  QUERY (keytomap: 'code') OF RegionType;
```

- A `CACHE` loads its whole source into memory at deploy. Without `refreshinterval` (in
  microseconds) it reloads only on restart. Join on its `keytomap` field.
- `CREATE EXTERNAL CACHE c (AdapterName: 'DatabaseReader', ConnectionURL: ..., Username: ...,
  Password: ..., Table: ..., Columns: ..., KeyToMap: ...) OF Type;` queries the database for each
  lookup; it holds no data. All seven properties are required.

## Open Processors and UDFs

```sql
LOAD OPEN PROCESSOR 'UploadedFiles/MyProcessor-1.0.jar';

CREATE OR REPLACE OPEN PROCESSOR AddRegion USING Global.MyProcessor (ConfigFile: 'UploadedFiles/enrich.json')
INSERT INTO EnrichedOrders FROM OrderChanges;
```

- `LOAD`/`UNLOAD OPEN PROCESSOR` need the admin role. Unload the old build before loading a new
  one; if the old classes still run afterwards, load the new build under a different file name.
- On 5.4.0.x both `INSERT INTO` and `FROM` are required.
- An Open Processor's own properties are whatever its documentation lists; a vault reference in
  them is resolved only if the processor resolves it.

## Writer table mapping

`Tables` on a writer maps source tables to target tables, `source,target`, several separated by
`;`:

```sql
CREATE OR REPLACE TARGET OrdersOut USING Global.DatabaseWriter (
  ConnectionURL: 'jdbc:postgresql://dwhost:5432/dw',
  Username: 'striim',
  Password: '[[shopvault.dwpass]]',
  Tables: "public.orders,dw.orders ColumnMap(order_status=status, src_op=@METADATA(OperationName), loaded_by='striim');public.order_lines,dw.order_lines",
  BatchPolicy: 'EventCount:1000,Interval:60',
  CommitPolicy: 'EventCount:1000,Interval:60'
) INPUT FROM OrderChanges;
```

- `ColumnMap(target=source, ...)`: target first.
- No `ColumnMap`: columns map by position.
- A `ColumnMap` with at least one entry: listed columns map as written, every other target column
  is matched to the source column of the same name, and a target column with no match gets no
  value, so it must allow null or have a default. One entry is enough to switch on name matching.
- The right-hand side is a source column, `@METADATA(key)`, `@USERDATA(key)`, `$ENV_VAR` or a
  single-quoted literal. Expressions are not allowed. A literal needs the whole `Tables` value in
  double quotes, as above.
- `KeyColumns(c1, c2)` names the key when the target table has none.
- Wildcards: `public.%,dw.%` maps every source table to the target table of the same name. On the
  source side `%` is a prefix match.
- Mapping one source table to two targets is not recoverable.

Batching and ignorable errors differ by writer:

| Writer | `BatchPolicy` default and format | `IgnorableExceptionCode` |
|---|---|---|
| DatabaseWriter | `'EventCount:1000,Interval:60'`, interval in whole seconds (`60s` is rejected); `CommitPolicy` takes the same form and must be at least the BatchPolicy | comma-separated: `DUPLICATE_ROW_EXISTS`, `NO_OP_UPDATE`, `NO_OP_DELETE`, `NO_OP_PKUPDATE`, `TABLE_NOT_FOUND`, or database error codes |
| SpannerWriter | `'EventCount: 1000, Interval: 60s'`, interval with unit `s m h d`; no `CommitPolicy` | semicolon-separated Spanner codes: `ALREADY_EXISTS;NOT_FOUND` |
| BigQueryWriter | `'eventCount:1000000, Interval:90'` | `TABLE_NOT_FOUND` and a few BigQuery-specific codes only |

An ignored error skips the event, logs a warning, and writes it to the exception store if the app
has one. Any other error halts the app.

## Secrets and variables

```sql
CREATE VAULT shopvault;
WRITE INTO shopvault (vaultKey: 'pgpass', vaultValue: 'secret', valueType: 'STRING');
CREATE OR REPLACE PROPERTYVARIABLE pgurl = 'jdbc:postgresql://pghost:5432/shop';
```

- A vault reference is the whole property value: `'[[shopvault.pgpass]]'` or
  `'[[ns.shopvault.pgpass]]'`. It is not resolved inside a longer string, and not inside a CQ.
- A property variable is referenced as `'$pgurl'` or `'$ns.pgurl'`.
- Apps read both at deploy; restart them after a change.
