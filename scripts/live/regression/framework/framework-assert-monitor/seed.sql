-- Initial load: DatabaseReader reads the table once at START, so pre_deploy is the right phase.
INSERT INTO ${TID}src (id, msg) VALUES ('1', 'alpha'), ('2', 'bravo'), ('3', 'charlie');
