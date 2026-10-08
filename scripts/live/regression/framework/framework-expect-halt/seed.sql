-- scripts/live/regression/hello-single/seed.sql
-- Runs into the qasource schema (db: postgres-source route, the default) before the app is deployed.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'alpha'), ('2', 'bravo'), ('3', 'charlie');
