BEGIN;
INSERT INTO ${TID}src (id, msg) VALUES (1, 'rolled back');
ROLLBACK;
