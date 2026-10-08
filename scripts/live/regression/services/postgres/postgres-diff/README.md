# postgres-diff

Exercises the diff tier with an initial-load lifecycle: the framework creates `src`+`tgt`
in separately owned source and target schemas, seeds `src` before deployment with the
three fixed text rows in `seed.sql`, then deploys `DatabaseReader(src) → DatabaseWriter(tgt)`.

The lifecycle waits for positive `baseline-landed` readiness, completes on a positive
`source-count`, and holds the result stable for two seconds within finite readiness and
completion deadlines. The top-level exact contract and exact diff declaration compare
the target as a multiset, so missing, extra, or duplicated rows fail rather than being
hidden by set convergence. Teardown drops only this run's schema and Striim
app+namespace.
