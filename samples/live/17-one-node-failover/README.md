# One-node failover

Run `striim-test run samples/live/17-one-node-failover` against the configured
Docker cluster with two app nodes and an agent. The second node must publish its
REST API port; the compose default is 9082, configurable with
`SLT_STRIIM_NODE_HTTP_HOST_PORT`. Existing stacks need the updated port mapping
before this sample runs. Run it without concurrent cases sharing these nodes.

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
