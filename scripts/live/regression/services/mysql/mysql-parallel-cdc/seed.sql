-- Parallel test seed data: unique worker ID per test
-- Each worker inserts rows tagged with its TID so cross-test contamination is detectable

INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}parallel_source (worker, data) VALUES
  ('worker_${TID}', 'parallel_record_1'),
  ('worker_${TID}', 'parallel_record_2'),
  ('worker_${TID}', 'parallel_record_3');
