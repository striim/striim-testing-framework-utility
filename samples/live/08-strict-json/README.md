# 08 Strict JSON payloads

A DatabaseReader copies two seeded rows to FileWriter with JSONFormatter. Every
compared column has a declared type: id is an integer token, name is a string token.
Lifecycle completion waits for both output lines in Docker mode.

```
striim-test run samples/live/08-strict-json
```

The hand-written golden follows the two inserts in seed.sql without conversion.
Deliberately broken input: change the id column in the source DDL to TEXT and quote
the seed ids. The emitted JSON ids are strings and must fail integer comparison.
An added JSON data field without a declaration must also fail, even if its value
looks like text. The framework refuses non-finite constants and incomplete JSON.
