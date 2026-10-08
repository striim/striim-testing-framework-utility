-- scripts/live/regression/oracle-diff/seed.sql
-- Rows in the Oracle source, read by DatabaseReader as an initial load (pre_deploy).
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('1', 'alpha');
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('2', 'bravo');
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('3', 'charlie');
