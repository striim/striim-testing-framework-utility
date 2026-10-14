# Transaction sessions

Run `striim-test run scripts/live/regression/framework/framework-transaction-seeds`.
The golden follows B commit, A savepoint rollback, recovery, then A commit.
Removing rollback_to must fail.

Recovery is enabled explicitly. Each begin declares bounded source key/slot
observations: commit evidence captures its transaction ID, server commit status,
source keys and replication-slot positions. Positions are recorded before
interruption and after restore. Failed row-count completion records target id
values; its deadline and both hand-derived expected rows stay unchanged.
