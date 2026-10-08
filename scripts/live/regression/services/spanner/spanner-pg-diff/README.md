# spanner-pg-diff

Cloud Spanner emulator, **PostgreSQL dialect**, same-backend source → target:
`SpannerBatchReader(src) → SpannerPGDialectWriter(tgt)`. Mirrors `oracle-diff` and
`postgres-diff` (source and target on the same engine) — no Postgres dependency.

The framework creates `src`+`tgt` in the Spanner PG-dialect database and seeds `src`
with three rows. `SpannerBatchReader` incrementally reads `src` (tracking position on
the `id` CheckColumn) and `SpannerPGDialectWriter` writes them to `tgt`. The diff
assertion polls until `tgt` matches `src` — both endpoints read via `SpannerAdmin`
(`source_db`/`target_db: spanner-postgres`).

Both reader and writer reach the emulator with `${SPANNER_PG_URL}`
(`autoConfigEmulator=true`).
