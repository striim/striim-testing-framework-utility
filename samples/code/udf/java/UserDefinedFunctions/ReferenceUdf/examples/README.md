# ReferenceUdf — Runnable examples

One folder per function. Each is **self-verifying**: the real function is asserted to turn the
fixture input into the fixture output — so these examples are documentation and a regression net
at once, and cannot rot.

### Fixture shape

| File | Meaning |
|---|---|
| `input.txt` | seeded as `data[0]` of a synthetic `WAEvent` passed to the function |
| `expected.txt` | the expected `data[0]` of the returned copy (every reference function here is a copy-adds-userdata transform, so `data[]` passthrough is checked alongside `userdata.processed=true`, unconditionally — see `ExamplesGuardTest`) |
| `app.tql` | a **self-contained runnable example** — Postgres source → this ONE function (called from a CQ) → a Postgres replica AND a `FileWriter`/`JSONFormatter` target, since `userdata` never lands in a relational column. Deploy it to see the function work end-to-end. |
| `ddl.sql` / `seed.sql` | the source/replica tables and the input row `app.tql` reads (the seed is `input.txt` as a column value) |

## Functions

| Function | Example | Does |
|---|---|---|
| [`ReferenceUdfMarkProcessed`](ReferenceUdfMarkProcessed/) | `"hello world"` → `"hello world"` + `userdata.processed=true` | Copies its input event unchanged and stamps `userdata.processed=true` on the copy — the UDF analog of `ReferenceOp`'s `copy-adds-userdata` sample. |

## I want to… → function

| I want to… | Use |
|---|---|
| See the minimal required shape (fail-safe try/catch, logging toggle, null-safety) for a new WAEvent-aware UDF | [`ReferenceUdfMarkProcessed`](ReferenceUdfMarkProcessed/) |
