-- Cloud Spanner GoogleSQL-dialect source + target tables.
-- id is INT64 so it can serve as the SpannerBatchReader CheckColumn.
-- DROP+CREATE so re-runs start from empty tables (idempotent).
DROP TABLE IF EXISTS ${TID}src;
DROP TABLE IF EXISTS ${TID}tgt;
CREATE TABLE ${TID}src (id INT64, msg STRING(MAX)) PRIMARY KEY (id);
CREATE TABLE ${TID}tgt (id INT64, msg STRING(MAX)) PRIMARY KEY (id);
