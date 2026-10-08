INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}orders (id, customer, status) VALUES
  (1001, 'Record01', 'new'),
  (1002, 'Record02', 'new'),
  (1003, 'Record03', 'new');
UPDATE ${MYSQL_SOURCE_SCHEMA}.${TID}orders SET status = 'shipped' WHERE id = 1003;
DELETE FROM ${MYSQL_SOURCE_SCHEMA}.${TID}orders WHERE id = 1002;
