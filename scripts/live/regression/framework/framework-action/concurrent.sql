-- Runs in a loop on its own thread for the whole action phase -- while the app is up, while it
-- is stopped, and while it is restarting. Rendered with the same token substitution as every
-- other SQL file, so the table is this test's own TID-prefixed one.
--
-- Fixed keys, ON CONFLICT DO NOTHING: the loop count is not knowable in advance, so the rows
-- are made idempotent instead. The FIRST iteration inserts all five; every later one is a no-op.
-- That makes the target's row count deterministic (3 seeded + 5) and lets `data: rows: 8` prove
-- the script ran at all -- which the diff alone cannot, since a script that never ran leaves
-- source and target trivially equal. Concurrent-op failures are logged, not failed (see
-- plugin.py), so without this the example would pass with the script broken.
INSERT INTO ${PG_SOURCE_SCHEMA}.${TID}src (id, msg) VALUES
  ('c1', 'during-cycle'), ('c2', 'during-cycle'), ('c3', 'during-cycle'),
  ('c4', 'during-cycle'), ('c5', 'during-cycle')
ON CONFLICT (id) DO NOTHING;
