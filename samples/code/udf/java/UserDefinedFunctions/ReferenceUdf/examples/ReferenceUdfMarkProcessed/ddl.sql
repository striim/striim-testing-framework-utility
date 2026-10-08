-- Source + replica tables for the ReferenceUdfMarkProcessed sample.
-- `doc` holds this example's input string; the replica's copy is byte-identical
-- (the function never touches data[] -- see ReferenceOp's copy-adds-userdata
-- sample, which this mirrors at the UDF-call layer instead of the OP layer).
DROP TABLE IF EXISTS ${TID}src;
DROP TABLE IF EXISTS ${TID}out;
CREATE TABLE ${TID}src (doc varchar);
CREATE TABLE ${TID}out (doc varchar);
