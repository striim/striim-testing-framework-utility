# Consistent snapshots

Run `striim-test run scripts/live/regression/framework/framework-snapshot-observer`.
Each observed pair of target tables must match empty, B committed, or A and B committed.

Recovery is enabled explicitly; commit evidence records source transaction IDs,
server commit status and both source key sets. Failed completion records target
id values. If completion fails before observer assertions, abandon retains the
full bounded history and the worker error, with UTC and monotonic timestamps.
The observer reports a worker failure when it occurs. Expected rows are unchanged.
