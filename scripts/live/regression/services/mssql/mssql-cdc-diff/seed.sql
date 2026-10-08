-- scripts/live/regression/services/mssql/mssql-cdc-diff/seed.sql
-- Inserted AFTER the app is RUNNING (when: post_start) so the CDC capture job
-- records them and MSSqlReader (StartPosition NOW) reads them. Runs as the qasource
-- data user (db: mssql-source).
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('1', 'alpha');
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('2', 'bravo');
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('3', 'charlie');
