# One-node failover

This case kills one app node hosting a single-owner flow, then requires placement
on its survivor and complete typed data. It needs Docker mode, `topology: cluster`,
two app nodes plus an agent, and the primary REST API (default 9080), which stays
available when this case kills the secondary. It does not require port 9082.
Set `SLT_ENABLE_NODE_FAULTS=1` only on a dedicated disposable stack;
without it this case skips before provisioning. Coordinate every stack user; `disabled_parallel: true` excludes concurrent
selection but does not lock the shared nodes. See
[the upgrade guide](../../../docs/UPGRADING.md) only if the secondary must serve
fallback reads; this sample does not require that stack recreation.

Run `striim-test run samples/live/17-one-node-failover` only after those prerequisites hold.

The bundled Docker recipe hosts its metadata database in the primary container.
The framework creates this case's owned worker group with only the secondary,
deploys the flow there, and verifies that initial owner. It then adds the primary
and verifies both members before writing the post-start seed. The flow remains
on the secondary until the fault; the primary is eligible to receive it while
metadata stays available. Killing a primary that hosts metadata is refused up
front with `fault-node-hosts-metadata`.

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

Broken-input check: change `servers` to `${STRIIM_PRIMARY_SERVER}` and
`add_after_deploy` to `${STRIIM_SECONDARY_SERVER}`. The initial owner hosts
metadata, so the framework must refuse with `fault-node-hosts-metadata` before
issuing a Docker fault. Expected rows and deadlines stay unchanged.

If hosting selection fails, its failed recovery record retains the placement
reads and Docker mapping before cleanup. Inspect MON and each queried server's
DESCRIBE response, the matched parent application's STATUS on that server,
owner names and truncation flags. No container is interrupted
when ownership is absent, ambiguous or cannot be read. The diagnostic record is
retained even when the constructor fails before the fault target exists.

Flow MON need not contain status. The owner check uses the flow deployment and
the parent application's RUNNING state reported by that same server. A stopped
or unreadable parent cannot qualify as a running owner.

Owner selection finishes before the seed. Progress polling reads status and count; once the window opens, one bounded check reads flow MON plus the selected server's DESCRIBE and STATUS, then rechecks count before issuance. Evidence retains the window-opening value/time and the post-interruption count. The target table CHECK sleeps 10ms per inserted row without changing its values; the 2400-row window therefore lasts about 24 seconds with one writer. The check belongs to the owned table and disappears when that table is dropped. If the window still closes, the case fails and retains both observations.

Each recovery case carries a byte-identical copy of the reviewed CSV in
`samples/live/14-progress-recovery/expected/rows.csv`. After a reviewed change to it, run
`python3 tools/prepare-recovery-goldens.py --write` from the framework checkout to update the copies.
The shared rows follow `(n, row-n)` for every integer from 1 through 5000.
