-- MariaDB user/schema setup for the integration test tier. Mounted read-only into
-- /docker-entrypoint-initdb.d/ by services/mariadb/compose.yaml and run against
-- MARIADB_DATABASE=intdb; also re-appliable by hand against your own MariaDB
-- (INT_MARIADB_HOST). Same accounts as services/mysql/sql/init.sql.
--
-- qasource = source-side tables, qatarget = target-side tables; both log in with
-- password 'striim'. A source table T and a same-named target table T coexist as
-- qasource.T / qatarget.T.

CREATE USER IF NOT EXISTS 'qasource'@'%' IDENTIFIED BY 'striim';
CREATE USER IF NOT EXISTS 'qatarget'@'%' IDENTIFIED BY 'striim';

CREATE SCHEMA IF NOT EXISTS qasource;
CREATE SCHEMA IF NOT EXISTS qatarget;

GRANT ALL PRIVILEGES ON qasource.* TO 'qasource'@'%';
GRANT ALL PRIVILEGES ON qatarget.* TO 'qatarget'@'%';

FLUSH PRIVILEGES;
