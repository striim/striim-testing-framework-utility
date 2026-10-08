# expected/ — ggtrail-cdc-file-diff

`ops.csv` is **generated, never hand-maintained**: the ggtrail runner emits it from
`../workload.yaml` with the pinned `seed: 42`, so the same workload always produces the same
op stream and therefore the same golden. Regenerate it with

```
cd scripts/live
python3 -m livetest.cli ggtrail --workload regression/services/ggtrail/ggtrail-cdc-file-diff/workload.yaml --out /tmp/gg-out
cp /tmp/gg-out/expected/ops.csv regression/services/ggtrail/ggtrail-cdc-file-diff/expected/ops.csv
```

Its columns must match what `app.tql`'s `ProjectOps` CQ emits — `TABLE_NAME,OP_TYPE,C0,C1,C2`
(the two META fields plus the widest column prefix common to all three tables) — because the
`file:` assertion compares the golden's rows against the JSON events' projected `data`. The
first live run on the Mac may need one calibration pass: GGTrailParser's `TableName` casing /
schema qualification and its rendering of the numeric and timestamp columns are only knowable
against a real Striim, so run the test once, read the reported diff, and align the generator's
`ops.csv` projection (not the golden by hand) with what the parser actually emits.

`ops.csv` is deliberately absent from this checkout — it lands here on first generation.
