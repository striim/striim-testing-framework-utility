-- Multi-table CDC test seed data
-- Parent-child relationships to test referential integrity

-- Customers (parent)
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}mt_customers (name, email) VALUES
  ('Record01 Group01', 'record01@example.com'),
  ('Record02 Group02', 'record02@example.com'),
  ('Record03 Group03', 'record03@example.com');

-- Orders (child of customers): 5 orders from 3 customers
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}mt_orders (customer_id, order_date, total) VALUES
  (1, '2024-01-15', 150.00),
  (1, '2024-02-10', 75.50),
  (2, '2024-01-20', 200.00),
  (3, '2024-02-05', 125.75),
  (2, '2024-02-15', 99.99);

-- Items (child of orders): 8 items from 5 orders
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}mt_items (order_id, product, quantity, price) VALUES
  (1, 'Widget A', 2, 25.00),
  (1, 'Widget B', 1, 100.00),
  (2, 'Gadget X', 3, 25.50),
  (3, 'Widget A', 1, 50.00),
  (3, 'Gadget Y', 2, 75.00),
  (4, 'Widget C', 1, 125.75),
  (5, 'Gadget X', 1, 50.00),
  (5, 'Gadget Z', 1, 49.99);
