# mssql-cdc-diff

The full SQL Server CDC path: **MSSqlReader** (native CDC, via the SQL Server Agent capture job)
→ DatabaseWriter, source and target tables in their own `qasource`/`qatarget` schemas of the
shared `qauser` database (like `postgres-cdc-diff`/`oracle-cdc-diff`). `qasource.SRC` is
CDC-enabled in `ddl_source.sql` (pre-deploy, run as the `qasource` data user — a `db_owner`
member, so no `sa` needed); `qatarget.TGT` is created in `ddl_target.sql` (run as the `qatarget`
data user). The seed runs **after** the app is RUNNING (`when: post_start`, reader
`StartPosition: NOW`) so the capture job records the inserts as change records; the diff tier
(source routed via `source_db: mssql-source`, target via `target_db: mssql-target`) asserts
`qatarget.TGT` catches up to `qasource.SRC`.

The reader connects as `${MSSQL_SOURCE_USER}` (qasource) and the writer as
`${MSSQL_TARGET_USER}` (qatarget) — both are `db_owner` members in `qauser`, so no `sa`
credentials are needed in the app itself. `sa` is still used by the framework for
`ensure_setup` (relaxing the `sa` password, creating `qauser`, `sp_cdc_enable_db`, and
creating the `qasource`/`qatarget` logins+users+schemas) — see `services/mssql/`.
