-- Small on purpose. This case exists to prove `diff_poll` is READ and APPLIED, not to measure
-- anything: a workload big enough to time would make the framework tier slow for every run.
INSERT INTO ${TID}src (id, msg) VALUES (1, 'a'), (2, 'b'), (3, 'c');
