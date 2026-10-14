# Transaction sessions

Run `striim-test run scripts/live/regression/framework/framework-transaction-seeds`.
The golden follows B commit, A savepoint rollback, recovery, then A commit.
Removing rollback_to must fail.

Recovery is enabled explicitly. Each begin declares bounded source key/slot
observations: commit evidence captures its transaction ID, server commit status,
source keys and replication-slot positions. Positions are recorded before
interruption and after restore. Failed row-count completion records target id
values; its deadline and both hand-derived expected rows stay unchanged.

STOP is triggered only after B has landed (0 < target count < 2), while A
remains uncommitted. A fresh recovery sentinel proves that the shared reader
and writer route transports data after START, before A commits. Expected
rows remain id 1/first and id 2/second. Broken-input check: suppress the recovery
marker with the conditional insert from sample 16; the case must fail before
A's post-recovery commit. Replacing b.sql with a no-op must fail the progress
barrier before STOP instead of measuring an interruption with no data in flight.
