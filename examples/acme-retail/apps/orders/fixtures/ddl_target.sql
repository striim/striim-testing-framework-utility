CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}orders_copy (
  order_id integer PRIMARY KEY,
  customer_id integer NOT NULL,
  amount decimal(12,2) NOT NULL,
  status varchar(16) NOT NULL
);
