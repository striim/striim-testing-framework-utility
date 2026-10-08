-- Target table, created as qatarget in the qatarget schema (db: postgres-target).
-- Same-named source/target tables can now coexist as qasource.T / qatarget.T.
CREATE TABLE ${TID}tgt (id varchar PRIMARY KEY, msg varchar);
