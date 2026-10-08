-- Target table, created as the qatarget data user (db: oracle-target). A same-named
-- source/target table can coexist as ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC / ${ORACLE_TARGET_SCHEMA}.${TID_ORACLE}TGT.
DROP TABLE IF EXISTS ${ORACLE_TARGET_SCHEMA}.${TID_ORACLE}TGT;
CREATE TABLE ${ORACLE_TARGET_SCHEMA}.${TID_ORACLE}TGT (ID VARCHAR2(20) PRIMARY KEY, MSG VARCHAR2(100));
