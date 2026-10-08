CREATE TABLE ${TID}customers_copy (
  customer_id integer PRIMARY KEY,
  email varchar(128),
  loyalty_tier varchar(16) NOT NULL,
  credit_limit numeric(12,2) NOT NULL
);
