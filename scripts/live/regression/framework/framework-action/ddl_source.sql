-- Source table, created as qasource in the qasource schema (db: postgres-source).
-- search_path is set to qasource by the harness, so the bare name lands there.
-- PK gives logical decoding a clean REPLICA IDENTITY.
CREATE TABLE ${TID}src (id varchar PRIMARY KEY, msg varchar);
