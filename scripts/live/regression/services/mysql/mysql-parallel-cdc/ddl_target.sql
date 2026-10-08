-- Parallel test target schema: receives replicated data
-- Each test writes to its own ${TID}-prefixed target table
DROP TABLE IF EXISTS ${MYSQL_TARGET_SCHEMA}.${TID}parallel_test_table;

CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}parallel_test_table (
  id INT PRIMARY KEY,
  worker VARCHAR(50),
  data VARCHAR(200),
  created_at TIMESTAMP
);

CREATE INDEX idx_worker ON ${MYSQL_TARGET_SCHEMA}.${TID}parallel_test_table(worker);
