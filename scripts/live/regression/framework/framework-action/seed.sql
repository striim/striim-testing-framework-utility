-- when: post_start -- committed once the app is RUNNING, so logical decoding captures it.
-- Three rows rather than one: a single row cannot distinguish "recovered everything" from
-- "recovered the last thing", which is the failure shape this phase exists to catch.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'before-stop-a');
INSERT INTO ${TID}src (id, msg) VALUES ('2', 'before-stop-b');
INSERT INTO ${TID}src (id, msg) VALUES ('3', 'before-stop-c');
