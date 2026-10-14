# One-node failover

Run `striim-test run samples/live/17-one-node-failover` against the configured
Docker cluster with two app nodes and an agent. The second node must publish its
REST API port; the compose default is 9082, configurable with
`SLT_STRIIM_NODE_HTTP_HOST_PORT`. Existing stacks need the updated port mapping
before this sample runs. Run it without concurrent cases sharing these nodes.

Before deployment, the framework confirms full membership again after node
join settles and prints the app-node and agent counts. Transient membership is
retried within the existing join budget; persistent missing membership skips
with `CLUSTER-READY` and retains node logs, rather than caching a single-node
topology after registration succeeded. A live run must execute the fault,
placement and equality assertions to pass this sample.

The named flow runs on one app node. While its target count is strictly between
100 and 2500, the framework rechecks its selected owner and kills only that
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

Owner selection finishes before the seed. Progress polling reads status and count; once the window opens, one bounded check reads flow MON plus the selected server's DESCRIBE and STATUS, then rechecks count before issuance. Evidence retains the window-opening value/time and the post-interruption count. The target table CHECK sleeps 10ms per inserted row without changing its values; the 2400-row window therefore lasts about24seconds with one writer. The check belongs to the owned table and disappears when that table is dropped. If the window still closes, the case fails and retains both observations.
