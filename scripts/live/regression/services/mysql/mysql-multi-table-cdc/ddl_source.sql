-- Multi-table CDC test source schema
-- Parent-child relationships: customers → orders → items
DROP TABLE IF EXISTS ${MYSQL_SOURCE_SCHEMA}.${TID}mt_items;
DROP TABLE IF EXISTS ${MYSQL_SOURCE_SCHEMA}.${TID}mt_orders;
DROP TABLE IF EXISTS ${MYSQL_SOURCE_SCHEMA}.${TID}mt_customers;

CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}mt_customers (
  customer_id INT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(100),
  email VARCHAR(100),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}mt_orders (
  order_id INT PRIMARY KEY AUTO_INCREMENT,
  customer_id INT,
  order_date DATE,
  total DECIMAL(10,2),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (customer_id) REFERENCES ${MYSQL_SOURCE_SCHEMA}.${TID}mt_customers(customer_id)
);

CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}mt_items (
  item_id INT PRIMARY KEY AUTO_INCREMENT,
  order_id INT,
  product VARCHAR(100),
  quantity INT,
  price DECIMAL(10,2),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (order_id) REFERENCES ${MYSQL_SOURCE_SCHEMA}.${TID}mt_orders(order_id)
);

CREATE INDEX idx_customer ON ${MYSQL_SOURCE_SCHEMA}.${TID}mt_orders(customer_id);
CREATE INDEX idx_order ON ${MYSQL_SOURCE_SCHEMA}.${TID}mt_items(order_id);
