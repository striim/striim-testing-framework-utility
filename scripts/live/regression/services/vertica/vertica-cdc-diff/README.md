# vertica-cdc-diff

Rows inserted into Vertica after the app is RUNNING (`seed … when: post_start`). Striim has no
log-based CDC reader for Vertica, so the stock way to capture new rows is
`IncrementalBatchReader` polling a check column (`CheckColumn: '%=id'`, every 5s). The source
table is empty at start; `DatabaseWriter` writes to the target, and the diff tier asserts the
target catches up. Schemas, users and the driver are as in `vertica-diff`.
