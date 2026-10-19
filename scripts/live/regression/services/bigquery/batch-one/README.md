# Headerless one-row BigQuery batch canary

A default CSV batch contains exactly the literal source row `(1,first)`.
APPENDONLY must retain row one exactly once; the golden is hand-derived from that
INSERT. Positive finite-load completion precedes typed exact comparison.

Run `striim-test doctor --case scripts/live/regression/services/bigquery/batch-one`
and `striim-test run scripts/live/regression/services/bigquery/batch-one`.
The bounded lane's installed-client exact-chunk-boundary exclusion applies.
