-- Source table, created as the qasource data user (db: oracle-source), before deploy.
-- Idempotent (Oracle 23 Free supports DROP ... IF EXISTS) for serial re-runs.
DROP TABLE IF EXISTS ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC;
CREATE TABLE ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID VARCHAR2(20) PRIMARY KEY, MSG VARCHAR2(100));
