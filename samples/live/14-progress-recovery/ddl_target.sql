CREATE TABLE ${TID}tgt (
  id integer PRIMARY KEY,
  msg varchar(64),
  CONSTRAINT ${TID}pace CHECK (pg_sleep(0.01) IS NOT NULL)
);
