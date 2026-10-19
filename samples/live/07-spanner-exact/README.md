# 07 Spanner exact rows

SpannerBatchReader copies a seeded Spanner table through SpannerWriter. Requires the
Spanner service and its emulator credential fixture, as described in
[Services](../../../docs/SERVICES.md). Run:

```
striim-test run samples/live/07-spanner-exact
```

The hand-written golden follows directly from the three inserts: ids 1, 2 and 3
carry alpha, bravo and charlie. Each must occur once, and each id must be an integer.
No live run was used to derive it.

Deliberately broken input: change the third insert's message to `wrong`. The exact
comparison must fail with a missing charlie row and an extra wrong row.

This sample polls exact reads without lifecycle completion. It proves final typed
content, not transaction visibility or completion of further input.
