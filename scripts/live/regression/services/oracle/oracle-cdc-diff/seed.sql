-- scripts/live/regression/oracle-cdc-diff/seed.sql
-- Runs AFTER the app is RUNNING (when: post_start) so LogMiner captures these
-- inserts as change records.
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('1', 'alpha');
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('2', 'bravo');
INSERT INTO ${ORACLE_SOURCE_SCHEMA}.${TID_ORACLE}SRC (ID, MSG) VALUES ('3', 'charlie');
