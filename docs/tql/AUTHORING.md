# Writing a TQL application

The steps from a requirement to a TQL file that deploys. Each step links to the page with the
detail: syntax in [REFERENCE.md](REFERENCE.md), rules in [RULES.md](RULES.md), complete examples in
[PATTERNS.md](PATTERNS.md).

## 1. Collect the requirement

Do not start writing until these are known. Ask for what is missing.

| Item | Example |
|---|---|
| Source: database, reader, tables, and their DDL | PostgreSQL, PostgreSQLReader, `public.orders`, `public.order_lines` |
| Load type | initial load, CDC, or both |
| Target: system, writer, tables, and their DDL | PostgreSQL warehouse, DatabaseWriter, `dw.orders` |
| Column mapping, source to target | `status` → `order_status`; `last_op` from the operation |
| Transformations and filters | trim `status`; drop rows where `region_code` is null |
| Lookups | region name from `public.regions`, about 200 rows, changes monthly |
| Functions and processors available | a UDF jar, an Open Processor and its configuration |
| Relationships between tables | `order_lines.order_id` references `orders.id` |
| Ordering, latency and volume | per-order order; under a minute; 2,000 changes/s at peak |
| Error policy | stop on any write error, except duplicates during the initial-load catch-up |
| Deployment | single server, cluster, or source on a Forwarding Agent |

With the source and target DDL, check every mapped column exists on both sides and that the types
convert.

## 2. Pick the shape

Find the closest pattern in [PATTERNS.md](PATTERNS.md). Most applications are pattern 1: one
reader, at most one CQ, one writer. Add a component only for a requirement that needs it, and write
down which. Initial load plus CDC is two applications (pattern 2).

## 3. Write the file

In this order, so each component exists before the next refers to it:

1. `CREATE OR REPLACE APPLICATION` with recovery, exception handler, exception store and
   autoresume (R1, R3).
2. Types and caches, if any.
3. The source.
4. CQs, routers, Open Processors, in data-flow order. Declare a stream only where
   [REFERENCE.md](REFERENCE.md) "Streams and types" says to.
5. The targets.
6. `END APPLICATION`, then `DEPLOY` and `START` if the file is run as a script.

Write mapping on the target (`ColumnMap`, `@METADATA`, `@USERDATA`) rather than in CQs (M2–M4).
Put a comment on any `data[n]` naming the column, and on any rule you break saying why.

## 4. Check it

Before deploying, go through [REVIEW.md](REVIEW.md). Then:

1. Run the file in the Striim console (`@/path/app.tql;`) or the Flow Designer's import. A syntax
   error names the line and column.
2. Deploy and start it against a test source and target.
3. Make one change of each kind the app handles (insert, update, delete, a row per table) and check
   the target row by row.
4. Stop it with `QUIESCE`, restart it, make more changes, and check nothing is lost or doubled.

To make steps 2–4 a repeatable test, see [Testing the TQL](#testing-the-tql).

## 5. Deliver

Deliver the whole file, every file it reads (configuration JSON, mapping files), and a short list
of what each component does and which rules it follows or breaks (D1–D4).

## Testing the TQL

This framework runs a TQL file against real databases and checks what it wrote. A test case needs
its TQL with the framework's tokens in place of connection values, object names that start with
`${TID}`, and the `CREATE NAMESPACE ${NS}` / `USE` / `DEPLOY` / `START` lines. Component names stay
within 21 characters because the framework's namespace is long. The details, and the samples to
copy, are in [WRITING-TESTS.md](../WRITING-TESTS.md) and [SERVICES.md](../SERVICES.md).
