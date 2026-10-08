-- Parallel test source schema: tests TID isolation
-- Isolation is by table-name prefix in the shared qasource/qatarget schemas:
-- ${TID} renders as t<9 hex>_ per test in parallel runs, empty in serial.
DROP TABLE IF EXISTS ${MYSQL_SOURCE_SCHEMA}.${TID}parallel_source;

CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}parallel_source (
  id INT PRIMARY KEY AUTO_INCREMENT,
  worker VARCHAR(50),
  data VARCHAR(200),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_worker ON ${MYSQL_SOURCE_SCHEMA}.${TID}parallel_source(worker);
