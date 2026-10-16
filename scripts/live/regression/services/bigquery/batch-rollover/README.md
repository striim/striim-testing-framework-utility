# BigQuery trailing partial batch

The literal source inserts `(1,row-1)` through `(5,row-5)`. With eventCount two,
the expected result is all five rows exactly once, including the trailing one-row
batch. The hand-derived golden does not depend on an emulator export or job count.
Actual load-job evidence records the rollover separately from final exact rows.

Run `striim-test doctor --case scripts/live/regression/services/bigquery/batch-rollover`
and `striim-test run scripts/live/regression/services/bigquery/batch-rollover`.
The bounded lane's installed-client exact-chunk-boundary exclusion applies.
