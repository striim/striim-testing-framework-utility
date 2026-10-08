# oracle-cdc-diff

The full Oracle CDC path: **OracleReader (LogMiner)** → DatabaseWriter, source
`QASOURCE.SRC` → target `QATARGET.TGT`. The reader connects as the `${ORACLE_CDC_USER}`
(striim / LogMiner) user; the writer as the `${ORACLE_TARGET_USER}` (qatarget) data user.
Seed runs **after** the app is RUNNING (`when: post_start`) so LogMiner captures the
inserts as change records; the diff tier (source via `source_db: oracle-source`, target
via `target_db: oracle-target`) asserts `QATARGET.TGT` catches up to `QASOURCE.SRC`.

Relies on the Oracle service having archivelog + supplemental logging + the common
`c##striim` LogMiner user enabled (see `services/oracle/`).

Key CDB/PDB details (live-validated): the OracleReader connects to the **CDB root**
(`${ORACLE_CDC_URL}` → `FREE`) as the common `${ORACLE_CDC_USER}` (`c##striim`) and
names the table **PDB-qualified** (`FREEPDB1.QASOURCE.SRC`) — LogMiner mines the whole
CDB and filters by `SRC_CON_NAME`, so an unqualified name resolves to `CDB$ROOT` and
matches nothing. The DatabaseWriter connects to the PDB (`${ORACLE_URL}` → `FREEPDB1`).
