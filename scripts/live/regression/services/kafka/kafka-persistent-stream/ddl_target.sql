-- Target table, created as qatarget in the qatarget schema (db: postgres-target).
DROP TABLE IF EXISTS ${TID}replicated_data CASCADE;
CREATE TABLE ${TID}replicated_data (
  id INT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  value DECIMAL(10, 2),
  created_at TIMESTAMP
);
