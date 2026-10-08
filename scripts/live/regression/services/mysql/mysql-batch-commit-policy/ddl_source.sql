-- Batch/commit policy test source schema
-- Tests multiple batches of inserts to verify transaction ordering
DROP TABLE IF EXISTS ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source;

CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source (
  id INT PRIMARY KEY AUTO_INCREMENT,
  batch_id INT,
  row_num INT,
  data VARCHAR(200),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_batch ON ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source(batch_id);
