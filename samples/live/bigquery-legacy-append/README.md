# Legacy streaming into local BigQuery

A finite Postgres DatabaseReader sends two rows through BigQueryWriter's legacy REST
`insertAll` path. The target table is created and owned by the framework before deploy.
The positive source baseline and target count witness completion; typed exact data
then proves values and multiplicity.

The golden follows directly from the literal source rows `(1,alpha)` and `(2,beta)`:
APPENDONLY performs no transformation, so each row must occur exactly once.

Build the pinned local image described in `scripts/live/services/bigquery/README.md`,
set `SLT_BIGQUERY_IMAGE`, then run:

```
striim-test doctor --case samples/live/bigquery-legacy-append
striim-test run samples/live/bigquery-legacy-append
```

The service supplies a throwaway per-stack key and a local HTTP PSC endpoint. No cloud
credentials or GCS staging are used. This proves finite initial load, not CDC completion,
replay safety, streaming insert-ID deduplication, Storage Write or cloud performance.
