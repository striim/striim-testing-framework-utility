-- Container-init provisioning so `docker compose up` exposes the users a developer
-- expects. The superuser (dbadmin, password striim) is created by `vcluster create_db` in
-- entrypoint.sh; this file creates the qasource/qatarget users and their same-named schemas.
--
-- Runs ONCE, right after create_db, unlike services/mssql's init.sql, which runs on every
-- boot: Vertica has no CREATE USER IF NOT EXISTS, and the users persist in the catalog
-- across stop/start, so a later boot has nothing to do.
CREATE USER qasource IDENTIFIED BY 'striim';
CREATE USER qatarget IDENTIFIED BY 'striim';
CREATE SCHEMA qasource AUTHORIZATION qasource;
CREATE SCHEMA qatarget AUTHORIZATION qatarget;
ALTER USER qasource SEARCH_PATH qasource, public;
ALTER USER qatarget SEARCH_PATH qatarget, public;
