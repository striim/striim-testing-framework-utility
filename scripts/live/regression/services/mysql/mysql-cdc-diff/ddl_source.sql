-- MySQL CDC Diff Test: source table for binlog capture
CREATE TABLE ${MYSQL_SOURCE_SCHEMA}.${TID}src (id VARCHAR(50) PRIMARY KEY, msg VARCHAR(200));
