# 10 Bounded absence after delivery

A PostgreSQLReader feeds FileWriter. Readiness and completion each insert a fresh
sentinel, observe its insert, delete it and observe the delete event in the same
file. Only then does a three-second observation window begin for source id 1.
The source insert of id 1 is rolled back; it must never occur in the output.

```
striim-test run samples/live/10-bounded-nothing
```

Docker mode is required for file reads. No golden comes from a run: the expected
count of id 1 is zero because the transaction rolls back. Sentinel events remain
in the append-only file, and the selector explicitly names the prohibited input.
Deliberately broken input: change ROLLBACK to COMMIT in rollback.sql. The insert
of id 1 must fail the absence assertion, even if it arrives late in the window.
Suppressing the marker route must fail lifecycle completion before absence runs.
