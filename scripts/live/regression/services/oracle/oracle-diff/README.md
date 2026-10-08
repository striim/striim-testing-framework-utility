# oracle-diff

The Oracle analogue of `postgres-diff`: **DatabaseReader → DatabaseWriter** across the
source (`QASOURCE.SRC`, read as `qasource`) and target (`QATARGET.TGT`, written as
`qatarget`) schemas, a non-CDC initial load. The source is seeded **before** deploy; the
reader queries it, the writer propagates, and the diff tier (source via
`source_db: oracle-source`, target via `target_db: oracle-target`) asserts `TGT` catches
up to `SRC`.

Requires the Oracle service (`requires: [oracle]`); no CDC / LogMiner involved — this
validates the plain Oracle reader/writer path and the `OraAdmin` DDL/seed/read wiring.
