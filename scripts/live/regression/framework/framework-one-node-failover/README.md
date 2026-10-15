# One-node failover

This case kills one app node hosting a single-owner flow, then requires placement
on its survivor and complete typed data. It needs Docker mode, `topology: cluster`,
two app nodes plus an agent, and the survivor REST port (default 9082).
Coordinate every stack user; `disabled_parallel: true` excludes concurrent
selection but does not lock the shared nodes. See
[the upgrade guide](../../../../../docs/UPGRADING.md) before reusing an older stack.

Run `striim-test run scripts/live/regression/framework/framework-one-node-failover` only after those prerequisites hold.

Before deployment, the framework confirms full membership again after node
join settles and prints the app-node and agent counts. Transient membership is
retried within the existing join budget; persistent missing membership skips
with `CLUSTER-READY` and retains node logs, rather than caching a single-node
topology after registration succeeded. A live run must execute the fault,
placement and equality assertions to pass this sample.

The named flow runs on one app node. While its target count is strictly between
100 and 5000, the framework verifies its current owner and kills only that
container ID. Read-only API calls authenticate to the surviving node. Restore
requires the flow to run on the survivor while its original owner remains down,
then starts the original container and proves a fresh post-recovery sentinel.
The placement assertion reads live deployment metadata and requires exactly the
surviving app node. Recovery uses the configured restore deadline.

The expected 5000 rows are derived from generate_series(1,5000): each id n has
msg row-n. No sentinel row belongs in the golden. Typed, counted equality and
lifecycle completion require all input pairs after failover.

Broken-input check: replace `WITH ${TID}flow ON ONE IN ${APP_GROUP}` with
`WITH ${TID}flow ON ALL IN ${APP_GROUP}`. The flow now has two owners, so the
hosting selector must fail with fault-missed before any Docker fault is issued.

If hosting selection fails, its failed recovery record retains the placement
reads and Docker mapping before cleanup. Inspect MON and each queried server's
DESCRIBE response, the matched parent application's STATUS on that server,
owner names and truncation flags. No container is interrupted
when ownership is absent, ambiguous or cannot be read. The diagnostic record is
retained even when the constructor fails before the fault target exists.

Flow MON need not contain status. The owner check uses the flow deployment and
the parent application's RUNNING state reported by that same server. A stopped
or unreadable parent cannot qualify as a running owner.
