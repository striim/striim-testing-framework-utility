CREATE TABLE ${TID}orders_copy (
  id integer PRIMARY KEY,
  customer varchar(64) NOT NULL,
  amount numeric(12, 2) NOT NULL,
  status varchar(16) NOT NULL
);
