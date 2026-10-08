-- when: post_start -- committed after the app reports RUNNING, so logical decoding captures it
-- and it lands in the target. Compare with seed_baseline.sql, which does not.
INSERT INTO ${TID}src (id, msg) VALUES ('2', 'after-running');
