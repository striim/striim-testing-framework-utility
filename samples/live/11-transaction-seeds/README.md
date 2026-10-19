# 11 Transaction seed sessions

Two source transactions overlap. B inserts id 2 and commits first. A inserts id 1,
sets a savepoint, inserts id 3 and rolls back to the savepoint. A stays open while
Striim is stopped and restored, then commits. The hand-written golden contains
only ids 1 and 2; id 3 is rolled back. The source commits are B then A.

```
striim-test run samples/live/11-transaction-seeds
```

Deliberately broken input: remove the rollback_to step. Id 3 then commits with A
and must fail the exact row multiset (and positive completion count).
The framework owns the two sessions and rolls back any still open on failure.

Recovery is enabled explicitly. Each begin declares bounded source key/slot
observations: commit evidence captures its transaction ID, server commit status,
source keys and replication-slot positions. Positions are recorded before
interruption and after restore. Failed row-count completion records target id
values; its deadline and both hand-derived expected rows stay unchanged.
