-- Cloud Spanner PostgreSQL-dialect source + target tables.
-- id is NUMERIC (bigint) so it can serve as the SpannerBatchReader CheckColumn.
-- DROP+CREATE so re-runs start from empty tables (idempotent).
DROP TABLE IF EXISTS ${TID}src;
DROP TABLE IF EXISTS ${TID}tgt;
CREATE TABLE ${TID}src (id bigint PRIMARY KEY, msg varchar);
CREATE TABLE ${TID}tgt (id bigint PRIMARY KEY, msg varchar);
