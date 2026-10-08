-- Container-init provisioning (runs once, on first cluster init, via
-- /docker-entrypoint-initdb.d — connected as the root superuser).
--
-- This file creates NEITHER the accounts nor the schemas the framework uses.
-- MySQLAdmin connects as root (service.yaml admin_user) and ensure_setup() creates
-- qasource and qatarget -- a FIXED pair of users and schemas, not per-test ones --
-- at runtime, so that a live (non-Docker) MySQL reached via SLT_MYSQL_HOST, where
-- this script never runs, is provisioned identically. Per-test isolation is by
-- ${TID} table-name PREFIX within those two schemas; see README.md alongside.
--
-- sltadmin = unused. Predates the move to root as the admin account.
--
-- striim   = unused by the provisioning path, but MySQLAdmin.create_schema() still
--            GRANTs to it. MySQL 8 does not auto-create a user on GRANT, so removing
--            the CREATE USER below would break that method against a fresh volume.
--
-- Both are kept because dropping them would force every existing MySQL volume to be
-- recreated for no behavioural gain.

CREATE USER IF NOT EXISTS 'sltadmin'@'%' IDENTIFIED BY 'striim';
GRANT ALL PRIVILEGES ON *.* TO 'sltadmin'@'%' WITH GRANT OPTION;

CREATE USER IF NOT EXISTS 'striim'@'%' IDENTIFIED BY 'striim';

-- Flush privileges to ensure all permission changes take effect.
FLUSH PRIVILEGES;
