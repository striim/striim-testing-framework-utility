-- Batch/commit policy test seed data
-- Multiple batches to verify commit ordering and atomicity

-- Batch 1: rows 1-3
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source (batch_id, row_num, data) VALUES
  (1, 1, 'batch_1_row_1'),
  (1, 2, 'batch_1_row_2'),
  (1, 3, 'batch_1_row_3');

-- Batch 2: rows 4-6
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source (batch_id, row_num, data) VALUES
  (2, 1, 'batch_2_row_1'),
  (2, 2, 'batch_2_row_2'),
  (2, 3, 'batch_2_row_3');

-- Batch 3: rows 7-9
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source (batch_id, row_num, data) VALUES
  (3, 1, 'batch_3_row_1'),
  (3, 2, 'batch_3_row_2'),
  (3, 3, 'batch_3_row_3');

-- Batch 4: rows 10-12
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}batch_source (batch_id, row_num, data) VALUES
  (4, 1, 'batch_4_row_1'),
  (4, 2, 'batch_4_row_2'),
  (4, 3, 'batch_4_row_3');
