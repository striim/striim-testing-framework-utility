# spanner-googlesql-diff

Cloud Spanner emulator, **GoogleSQL dialect**, same-backend source → target:
`SpannerBatchReader(src) → SpannerWriter(tgt)`. Mirrors `oracle-diff` and `postgres-diff`
(source and target on the same engine) — no Postgres dependency.

The framework creates `src`+`tgt` in the Spanner GoogleSQL database and seeds `src` with
three rows. `SpannerBatchReader` incrementally reads `src` (tracking position on the `id`
CheckColumn) and `SpannerWriter` writes them to `tgt`. The diff assertion polls until
`tgt` matches `src` — both endpoints read via `SpannerAdmin`
(`source_db`/`target_db: spanner-google`).

Both reader and writer reach the emulator with `${SPANNER_GSQL_URL}`
(`autoConfigEmulator=true`, no credentials).
