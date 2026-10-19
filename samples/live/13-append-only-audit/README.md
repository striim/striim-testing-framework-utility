# 13 Append-only audit target

DatabaseReader emits two rows. A CQ converts them to a locally declared typed
stream, so DatabaseWriter records deliveries as inserts. The audit table has no
unique key; upserts are explicitly disabled. The framework refuses triggers,
rules and unique/exclusion indexes, installs an owned guard rejecting destructive writes, and
uses counted exact comparison. The golden follows the two seed inserts by hand.

```
striim-test run samples/live/13-append-only-audit
```

Deliberately broken input: add a second DatabaseWriter target consuming the same
`${TID}alog` stream and inserting into the same audit table with the same false upsert
flags. Each source row is then delivered twice. Completion/count or exact
multiset comparison must fail; a keyless audit table cannot absorb those inserts.
Enabling either upsert flag must be refused before deployment.
For a business target that merges by key, keep it and add this audit branch
alongside it; its golden describes deliveries, not only converged business state.
