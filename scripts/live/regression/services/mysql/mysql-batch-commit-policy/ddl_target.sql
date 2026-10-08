-- Batch/commit policy test target schema
-- Receives replicated batches in commit order
DROP TABLE IF EXISTS ${MYSQL_TARGET_SCHEMA}.${TID}batch_test_table;

CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}batch_test_table (
  id INT PRIMARY KEY,
  batch_id INT,
  row_num INT,
  data VARCHAR(200),
  created_at TIMESTAMP
);

CREATE INDEX idx_batch ON ${MYSQL_TARGET_SCHEMA}.${TID}batch_test_table(batch_id);
