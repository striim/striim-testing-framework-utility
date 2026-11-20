# 20 Spanner JSON column

SpannerBatchReader copies a seeded Spanner table with a `JSON` column through SpannerWriter, and
`assert.json` compares the target's column with a hand-written golden. Requires the Spanner
service and its emulator credential fixture, as described in [Services](../../../docs/SERVICES.md).
Run:

```
striim-test run samples/live/20-spanner-json
```

The golden follows from the two inserts: row 1 carries a string, a number, `true` and a two-element
array; row 2 a zero, `false`, an empty array and a `null` member. `assert.json` compares each
row's value by type (`1` is not `"1"`, `true` is not `1`) and arrays in order, and its key sets
first, so a missing or extra row fails before any value is read. No live run was used to derive it.

Deliberately broken input: change row 1's tags to `["b", "a"]` in `seed.sql`. The comparison
must fail at `tags[0]`, since array order is part of the value.
