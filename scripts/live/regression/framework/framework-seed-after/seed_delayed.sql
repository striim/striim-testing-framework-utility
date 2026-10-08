-- when: post_start + after: 10s -- the runner waits ten seconds past RUNNING before running
-- this file, printing a heartbeat while it waits.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'delayed');
