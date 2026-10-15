# A fresh marker after restore

The readiness marker proves capture, a progress fault interrupts the 5000 input
pairs, and recover.sentinel: true injects a new insert/delete marker after restore.
Only a marker observed present then absent allows post-recovery steps and assertions.
The golden is derived from ids 1..5000 and row- plus id, as in sample 14.
The recovery record retains the marker witness and bounded completion deadline.

Run `striim-test run samples/live/16-recovery-sentinel` on a configured Striim host.

Broken-input check: replace sentinel_insert.sql with this conditional insert:

```sql
INSERT INTO ${TID}src (id, msg)
SELECT ${SENTINEL_ID}, '${SENTINEL_TAG}'
WHERE '${SENTINEL_TAG}' NOT LIKE '%-recovery_%';
```

Readiness still works and all data can arrive, but the post-recovery marker is
suppressed. The case must fail post-recovery-sentinel-missed before assertions.

The target delays each insert by 10 ms. This keeps the declared 100-to-2500-row
window open for at least 24 seconds while the asynchronous STOP takes effect.
The fault bounds, deadlines and all 5000 expected rows remain unchanged.

The recovery cases share the reviewed CSV in `samples/live/14-progress-recovery/expected/rows.csv`.
From the framework checkout, run `python3 tools/prepare-recovery-goldens.py` before
copying or running these cases. Test environment setup also prepares the copies.
The shared rows follow `(n, row-n)` for every integer from 1 through 5000.
