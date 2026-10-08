-- MySQL CDC Diff Test: target table for replicated data
CREATE TABLE ${MYSQL_TARGET_SCHEMA}.${TID}tgt (id VARCHAR(50) PRIMARY KEY, msg VARCHAR(200));
