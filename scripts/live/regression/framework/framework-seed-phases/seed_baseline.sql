-- when: pre_deploy -- committed before the app is deployed.
--
-- This row reaches the SOURCE and stops there. The reader is PostgreSQLReader, which streams
-- from its position at START, so a commit that predates the app is never replayed even though
-- slot.sql created the replication slot back in the ddl phase. Seeding a CDC test's data
-- pre_deploy is therefore a silent no-op at the target -- the exact mistake `when: post_start`
-- prevents, and the reason this file's twin uses it.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'before-deploy');
