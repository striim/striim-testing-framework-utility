# Oracle observer routes

Run `striim-test run scripts/live/regression/services/oracle/oracle-observer`
after the [Oracle observer setup](../../../../../../docs/SERVICES.md#oracle-observers).
Rebuild the Oracle image and use a fresh service, or have the database administrator
apply the documented direct grants to an existing data PDB.

The initial-load app copies the three hand-written source rows to the target.
The target user creates a filtered session view and a log table; the admin route
creates a database logon trigger in the same PDB. The case requires matching data,
at least one writer logon, a readable session view and no recorded framework probes.
Observer hooks are removed even after a failure or with resources retained.

Broken-input check: remove the program filter from the trigger while keeping the
PROBES view unchanged. Target probes then appear in the log and its zero-row
assertion must fail. Removing the direct SYS.V_$SESSION grant instead must fail
observer view or trigger creation. Neither change alters the expected source rows.
