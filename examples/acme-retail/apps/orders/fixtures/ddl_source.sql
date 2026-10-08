CREATE TABLE ${TID}orders (
  order_id integer PRIMARY KEY,
  customer_id integer NOT NULL,
  amount numeric(12,2) NOT NULL,
  status varchar(16) NOT NULL
);
