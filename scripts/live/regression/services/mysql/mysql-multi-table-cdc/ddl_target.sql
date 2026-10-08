-- Multi-table CDC test target schema
-- Receives replicated data from all three tables
DROP TABLE IF EXISTS ${MYSQL_TARGET_SCHEMA}.${TID}mt_items;
DROP TABLE IF EXISTS ${MYSQL_TARGET_SCHEMA}.${TID}mt_orders;
DROP TABLE IF EXISTS ${MYSQL_TARGET_SCHEMA}.${TID}mt_customers;

CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}mt_customers (
  customer_id INT PRIMARY KEY,
  name VARCHAR(100),
  email VARCHAR(100),
  created_at TIMESTAMP
);

CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}mt_orders (
  order_id INT PRIMARY KEY,
  customer_id INT,
  order_date DATE,
  total DECIMAL(10,2),
  created_at TIMESTAMP
);

CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}mt_items (
  item_id INT PRIMARY KEY,
  order_id INT,
  product VARCHAR(100),
  quantity INT,
  price DECIMAL(10,2),
  created_at TIMESTAMP
);

CREATE INDEX idx_customer ON ${MYSQL_TARGET_SCHEMA}.${TID}mt_orders(customer_id);
CREATE INDEX idx_order ON ${MYSQL_TARGET_SCHEMA}.${TID}mt_items(order_id);
