CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}retail_events (
  event_id integer PRIMARY KEY,
  event_type varchar(32) NOT NULL,
  order_id integer NOT NULL
);
