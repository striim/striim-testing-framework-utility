# A fresh marker after restore

Run `striim-test run samples/live/16-recovery-sentinel` on a configured Striim host.
The readiness marker proves capture, a progress fault interrupts the 5000 input
pairs, and recover.sentinel: true injects a new insert/delete marker after restore.
Only a marker observed present then absent allows post-recovery steps and assertions.
The golden is derived from ids 1..5000 and row- plus id, as in sample 14.
The recovery record retains the marker witness and bounded completion deadline.

Broken-input check: replace sentinel_insert.sql with this conditional insert:

```sql
INSERT INTO ${TID}src (id, msg)
SELECT ${SENTINEL_ID}, '${SENTINEL_TAG}'
WHERE '${SENTINEL_TAG}' NOT LIKE '%-recovery_%';
```

Readiness still works and all data can arrive, but the post-recovery marker is
suppressed. The case must fail post-recovery-sentinel-missed before assertions.
