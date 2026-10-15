# Oracle observer routes

Run `striim-test run scripts/live/regression/services/oracle/oracle-observer`
after the [Oracle observer setup](../../../../../../docs/SERVICES.md#oracle-observers).
Rebuild the Oracle image and use a fresh service, or have the database administrator
apply the documented direct grants to an existing data PDB.

The initial-load app copies the three hand-written source rows to the target.
The admin route creates a database logon trigger that records every target-user
logon. PROBES must contain at least one `striim-test-observer` row, proving the
framework's login tag reaches `V$SESSION.PROGRAM`. WRITERS must contain at least
one other program, proving writer logons remain visible. The source/target diff
checks the three rows independently. The case makes no exact session-count claim.

Observer views and triggers see every session of the data user in the PDB,
including other cases and runs. Run serially on an unshared service; the case is
`disabled_parallel` for that reason. Exact counts require the same isolation.
Observer hooks are removed even after a failure or with resources retained. The
trigger uses dynamic SQL and swallows runtime errors so a missing log table cannot
invalidate it or block another login. If any observer fails teardown, Oracle
tables are preserved and recorded as verification gaps.

Broken-input check: in an isolated framework copy, remove only the `program=`
argument from `livetest.oraadmin._default_connect`, leaving this case unchanged.
PROBES must fail its `min_rows: 1` assertion because no framework connection carries
the tag. Neither this change nor the check alters the expected source rows.
A trigger or view that compiles with errors must instead fail immediately with
`oracle-ddl-compilation-error`; the framework attempts to drop it before raising.
