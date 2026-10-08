BEGIN;
INSERT INTO ${TID}orders (order_id, customer_id, amount, status) VALUES
  (1001, 2001, 25.00, 'placed'),
  (1002, 2002, 7.50, 'placed'),
  (1003, 2003, 120.25, 'placed');
COMMIT;
BEGIN;
UPDATE ${TID}orders SET amount = 118.00, status = 'shipped' WHERE order_id = 1003;
COMMIT;
BEGIN;
DELETE FROM ${TID}orders WHERE order_id = 1002;
COMMIT;
