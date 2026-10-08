-- The postgres-source route sets search_path to ${PG_SOURCE_SCHEMA} (qasource in the Docker service).
CREATE TABLE ${TID}src (
  id integer PRIMARY KEY,
  name varchar(32) NOT NULL
);
