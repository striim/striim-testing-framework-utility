# Transaction sessions

Run `striim-test run scripts/live/regression/framework/framework-transaction-seeds`.
The golden follows B commit, A savepoint rollback, recovery, then A commit.
Removing rollback_to must fail.
