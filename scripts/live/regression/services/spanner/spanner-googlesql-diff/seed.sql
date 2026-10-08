-- Seeds the Spanner GoogleSQL-dialect source table before the app is deployed.
INSERT INTO ${TID}src (id, msg) VALUES (1, 'alpha'), (2, 'bravo'), (3, 'charlie');
