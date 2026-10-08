-- The order is the test contract: insert the fixed set, update id 102, then delete all three.
BEGIN;
INSERT INTO ${TID}src (id, msg) VALUES
  (101, 'alpha'),
  (102, 'bravo'),
  (103, 'charlie');
COMMIT;

BEGIN;
UPDATE ${TID}src SET msg = 'bravo-updated' WHERE id = 102;
COMMIT;

BEGIN;
DELETE FROM ${TID}src WHERE id IN (101, 102, 103);
COMMIT;
