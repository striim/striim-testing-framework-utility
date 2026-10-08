-- MySQL CDC Diff Test: seed data inserted post-start for binlog capture
INSERT INTO ${MYSQL_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES ('1', 'alpha'), ('2', 'bravo'), ('3', 'charlie');
