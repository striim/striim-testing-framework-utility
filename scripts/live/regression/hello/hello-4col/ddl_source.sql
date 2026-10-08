-- scripts/live/regression/hello/hello-4col/ddl_source.sql
-- 4-column source table (all varchar, mirroring transform's customer shape).
CREATE TABLE ${TID}src (id varchar, first_name varchar, last_name varchar, nickname varchar);
