# spanner-cdc-diff

`spanner-googlesql-diff` with the rows inserted after the app is RUNNING (`seed … when:
post_start`). Striim 5.4 ships no stock Spanner change-stream reader, so the stock way to capture
new rows is `SpannerBatchReader`'s incremental poll on a check column (`CheckColumn: '%=id'`). The
source table is empty at start; the diff tier asserts that the rows inserted afterwards reach the
target through `SpannerWriter`.
