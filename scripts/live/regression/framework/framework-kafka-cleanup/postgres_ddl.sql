-- Source table, created as qasource in the qasource schema (db: postgres-source).
-- search_path is set to qasource by the harness, so the bare name lands there.
DROP TABLE IF EXISTS ${TID}source_data CASCADE;
CREATE TABLE ${TID}source_data (
  id INT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  value DECIMAL(10, 2),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
