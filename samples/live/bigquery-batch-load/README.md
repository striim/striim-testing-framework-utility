# Direct CSV batch loads into local BigQuery

A finite Postgres DatabaseReader sends `(1,first)` and `(2,second)` through
BigQueryWriter with `StreamingUpload=false`. The writer keeps its default pipe,
NULL marker, UTF-8 and double-quote CSV settings. It uploads directly to BigQuery;
GCS is not staging for this path.

APPENDONLY performs no transformation: the hand-derived golden contains those two
rows exactly once. A positive finite source baseline and target count witness
completion before typed exact values and multiplicity are compared. The target
is created and owned by the framework before deploy.

Build the pinned service described in `scripts/live/services/bigquery/README.md`,
set `SLT_BIGQUERY_IMAGE`, then run:

```
striim-test doctor --case samples/live/bigquery-batch-load
striim-test run samples/live/bigquery-batch-load
```

The bounded L2 lane excludes the installed Java client 2.59.0 exact-chunk-boundary
failure. Its disabled native-client regression still expects one exact row;
padding, chunk-size or golden workarounds are not part of this sample. This finite
load does not establish CDC MERGE, recovery, Storage Write or cloud equivalence.
