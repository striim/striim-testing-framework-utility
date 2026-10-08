# vertica-diff

The Vertica analogue of `mssql-diff`/`mysql-diff`: **DatabaseReader → DatabaseWriter** within the
shared `sltdb` database, a non-CDC initial load, source and target tables in their own
`qasource`/`qatarget` schemas (`qasource.src` → `qatarget.tgt`). The reader connects as
`${VERTICA_SOURCE_USER}` (qasource), the writer as `${VERTICA_TARGET_USER}` (qatarget), so no
superuser credentials are needed in the app. The source is seeded **before** deploy; the reader
queries it, the writer propagates, and the diff tier (source routed via
`source_db: vertica-source`, target via `target_db: vertica-target`) asserts `qatarget.tgt`
catches up to `qasource.src`.

Requires the vertica service (`requires: [vertica]`) and a Striim image carrying
`vertica-jdbc` (`services/striim/download-dependencies.sh`). It validates the Striim JDBC path to
Vertica and the `VerticaAdmin` DDL/seed/read wiring.

```bash
STRIIM_PASS=striim python -m pytest -m "live and vertica" regression/services/vertica/vertica-diff/test.yaml
```
