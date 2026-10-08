-- scripts/live/regression/services/mssql/mssql-diff/seed.sql
-- Rows in the SQL Server source, read by DatabaseReader as an initial load (pre_deploy).
-- Runs as the qasource data user (db: mssql-source).
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('1', 'alpha');
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('2', 'bravo');
INSERT INTO ${MSSQL_SOURCE_SCHEMA}.${TID}SRC (ID, MSG) VALUES ('3', 'charlie');
