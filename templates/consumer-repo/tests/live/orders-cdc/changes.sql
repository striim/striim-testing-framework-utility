BEGIN;
INSERT INTO ${TID}orders (id, customer, amount, status) VALUES
  (1001, 'Record01', 25.00, 'new'),
  (1002, 'Record02', 7.50, 'new'),
  (1003, 'Record03', 120.25, 'new');
COMMIT;

BEGIN;
UPDATE ${TID}orders SET status = 'shipped', amount = 118.00 WHERE id = 1003;
COMMIT;

BEGIN;
DELETE FROM ${TID}orders WHERE id = 1002;
COMMIT;
