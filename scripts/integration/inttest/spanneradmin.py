"""Spanner admin client for the integration tier -- ported from
`scripts/live/livetest/spanneradmin.py`,
trimmed to what this tier actually needs.

The live tier's `SpannerAdmin` also reads rows back for its `assert.file`/`assert.json`
tiers (`count_rows`, `select_rows`, `select_json_rows`, plus the `_rows_to_dicts`/
`_jsonify` helpers that back them). This tier asserts exclusively through
`waevent.compare` on emitted events and never reads a DB row directly, so that half is
deliberately NOT ported here -- porting it would also drag in
`livetest.sqlutil.coerce_cell`, which has no `inttest` equivalent.

What IS ported, verbatim except as noted:
  - `ensure()`      -- idempotent instance+database creation, with the dialect-mismatch
                       self-heal (a long-lived `isolation: none` emulator can otherwise
                       get stuck with a database created under the wrong dialect).
  - `run_sql`       -- as `run_statements(list[str])`, since `dbroutes.run_sql_text`
                       already splits the rendered SQL into statements; this drops the
                       live version's internal `_split_ddl` re-split.
  - `drop_test_tables` -- teardown, used with the per-test `${TID}` prefix.

`ensure`/`admins_for` (module-level, ported from `livetest/plugin.py`'s `_spanner_ensure`/
`_spanner_admins`) live here rather than in `inttest/plugin.py` because both
`dbroutes._run_spanner` and `plugin.py`'s per-test provisioning need them, and
`dbroutes` must not import `plugin`.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import re
import time
from pathlib import Path

from inttest import paths

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


class SpannerAdmin:
    def __init__(self, dsn: dict, database=None, client=None):
        # dsn: {project, instance, database, dialect, emulator_host}
        #   dialect: "google_standard_sql" | "postgresql"
        self.dsn = dsn
        self._database = database  # injectable google Database (query/ddl) -- unit-test seam
        self._client = client  # injectable google Client (ensure) -- unit-test seam

    # --- google client plumbing (skipped when fakes are injected) ---------------
    def _spanner_client(self):
        if self._client is not None:
            return self._client
        os.environ.setdefault("SPANNER_EMULATOR_HOST", self.dsn["emulator_host"])
        from google.cloud import spanner

        return spanner.Client(project=self.dsn["project"])

    def _db(self):
        if self._database is not None:
            return self._database
        return self._spanner_client().instance(self.dsn["instance"]).database(self.dsn["database"])

    # --- service bootstrap ------------------------------------------------------
    def ensure(self) -> None:
        # Create the emulator instance (idempotent) and this database with its dialect.
        client = self._spanner_client()
        cfg = f"projects/{self.dsn['project']}/instanceConfigs/emulator-config"
        inst = client.instance(self.dsn["instance"], configuration_name=cfg, display_name="slt")
        if not inst.exists():
            inst.create().result(120)
        from google.cloud.spanner_admin_database_v1 import DatabaseDialect

        dialect = (
            DatabaseDialect.POSTGRESQL
            if self.dsn["dialect"] == "postgresql"
            else DatabaseDialect.GOOGLE_STANDARD_SQL
        )
        db = inst.database(self.dsn["database"], database_dialect=dialect)
        if db.exists():
            # The dialect is fixed at creation and can't be changed in place. On a
            # long-lived shared emulator (isolation: none) a database created before
            # dialect-aware provisioning existed -- or under the wrong dialect from a
            # stray earlier run -- stays stuck: every DDL statement written for the
            # intended dialect syntax-errors against the wrong grammar forever. Detect
            # and self-heal rather than silently reusing a mismatched database.
            db.reload()
            if db.database_dialect != dialect:
                db.drop()
                db = inst.database(self.dsn["database"], database_dialect=dialect)
                db.create().result(120)
        else:
            db.create().result(120)

    # --- ddl + dml (uniform-ish with _run_postgres/_run_oracle) -----------------
    def run_statements(self, statements: list[str]) -> None:
        # Route DDL (CREATE/DROP/ALTER) through update_ddl and DML (INSERT/UPDATE/DELETE)
        # through a read-write transaction (execute_update). Statements arrive
        # pre-split (dbroutes.split_statements already did that), unlike the live
        # tier's run_sql, which splits internally.
        ddl, dml = [], []
        for stmt in statements:
            head = stmt.split(None, 1)[0].upper() if stmt.split() else ""
            (ddl if head in ("CREATE", "DROP", "ALTER") else dml).append(stmt)
        if ddl:
            # ⚠ Serialised across xdist workers. The emulator refuses a
            # schema change while another schema change OR read-write transaction is in
            # flight on the database -- `FAILED_PRECONDITION: Schema change operation
            # rejected because a concurrent schema change operation or read-write
            # transaction is already in progress` -- and under `-n 2` two cases' DDL
            # collide often enough that every parallel run of the writer tier failed
            # exactly one case this way (a different one each time, always green serially).
            # A per-database OS lock around the DDL, and around the seed DML that follows
            # it in the same call, is what the emulator itself lacks. Real Spanner queues
            # schema changes; the lock is then merely unnecessary.
            with self._ddl_lock():
                self._update_ddl(ddl)
                if dml:
                    self._run_dml(dml)
            return
        if dml:
            self._run_dml(dml)

    #: The emulator's refusal. Real Spanner never says this for a queued schema change.
    _CONCURRENT = "concurrent schema change operation or read-write transaction"

    def _update_ddl(self, ddl: list[str], budget_s: float = 120.0) -> None:
        # ⚠ The lock is half of the fix. The emulator ALSO refuses a schema change while a
        # READ-WRITE TRANSACTION is open on the database, and under `-n 2` that transaction
        # is the other worker's writer JVM mid-window -- a process no harness-side lock can
        # serialise. Measured on the first locked `-n 2` run: two cases still failed, both on
        # spanner-postgres, both with the refusal. A writer's transaction is short (one
        # window), so the refusal is retried with a small backoff inside a budget; anything
        # else, or the budget spent, raises as before.
        deadline = time.monotonic() + budget_s
        delay = 0.25
        while True:
            try:
                self._db().update_ddl(ddl).result(120)
                return
            except Exception as e:  # noqa: BLE001 -- narrowed by message below
                if self._CONCURRENT not in str(e) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 4.0)

    def _run_dml(self, dml: list[str]) -> None:
        def _txn(txn):
            for s in dml:
                txn.execute_update(s)

        self._db().run_in_transaction(_txn)

    def _ddl_lock_path(self) -> Path:
        # One lock file per emulator database, beside the harness's other state files.
        # Keyed by EMULATOR as well as database, and stack-scoped like every other state
        # file: two INT_STACK_PREFIX stacks run two emulators with the same instance and
        # database names, and one lock across them would let a retry sleeping under it (up
        # to budget_s, waiting on its own emulator's transaction) stall the other stack's
        # DDL for nothing.
        from inttest.services import state_name  # lazy: services imports docker deps

        key = f"{self.dsn.get('emulator_host', '')}-{self.dsn['instance']}-{self.dsn['database']}"
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", key)
        return paths.state_dir() / state_name(f".int-spanner-ddl-{name}.lock")

    @contextlib.contextmanager
    def _ddl_lock(self):
        # fcntl, like the session lock: released by the kernel if the holder dies, so a
        # SIGKILLed worker cannot strand the others.
        with open(self._ddl_lock_path(), "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def query_rows(self, sql: str) -> list[list]:
        """One query's rows, for `dbroutes.query_rows` -- the target tier's read half."""
        _, rows = self._query(sql)
        return [list(row) for row in rows]

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop user tables in this admin's database. prefix ""
        drops ALL user tables (serial runs); a non-empty prefix (the per-test ${TID})
        drops only this test's tables so a concurrent sibling's tables survive.
        Filters to the user schema ('' for GoogleSQL, 'public' for the PostgreSQL
        dialect) so information_schema/spanner_sys never match. Multi-pass: an
        interleaved child / FK parent can only drop after its dependents, so retry
        until no progress. Best-effort teardown."""
        _, rows = self._query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema IN ('', 'public')"
        )
        tables = [r[0] for r in rows]
        if prefix:
            low = prefix.lower()
            tables = [t for t in tables if t.lower().startswith(low)]
        remaining = [t for t in tables if _IDENT.match(t)]
        while remaining:
            failed = []
            for t in remaining:
                try:
                    with self._ddl_lock():  # a sibling's CREATE must not collide
                        self._update_ddl([f"DROP TABLE {t}"], budget_s=30.0)
                except Exception:  # noqa: BLE001 -- dependency ordering; retried below
                    failed.append(t)
            if len(failed) == len(remaining):
                break  # no progress -- leave the rest (best-effort)
            remaining = failed

    def _query(self, sql: str):
        with self._db().snapshot() as snap:
            rs = snap.execute_sql(sql)
            rows = list(rs)
            cols = [f.name for f in rs.fields]
            return cols, _decode_bytes_columns(rs.fields, rows)


def _decode_bytes_columns(fields, rows: list) -> list:
    """Base64-decode BYTES columns, which this client hands back still encoded.

    ⚠ MEASURED, and it read as a WRITER defect for a day. Spanner's wire format carries BYTES
    base64-encoded, and the Python client does NOT decode it: a column holding the nine bytes
    `00 01 02 03 ff fe fd ca 7e` comes back as `b'AAECA//+/cp+'` -- the base64 TEXT, as bytes.
    `_jsonable_rows` then renders those twelve ASCII bytes as hex, so an `assert.target:`
    comparison saw `41414543412f2f2b2f63702b` and reported stored data that differs from what
    was written.

    The database is correct. Reading the same rows over JDBC returns the original nine bytes,
    which is what proved the corruption was on this side. Without this decode the harness cannot
    assert about a binary column on Spanner at all -- it compares its own encoding artefact.

    Only fields the result set TYPES as BYTES are decoded; a STRING column that happens to look
    like base64 is left alone.
    """
    from google.cloud.spanner_v1 import TypeCode  # lazy, as everywhere else in this module

    indexes = [i for i, f in enumerate(fields) if f.type_.code == TypeCode.BYTES]
    if not indexes:
        return rows
    out = []
    for row in rows:
        cells = list(row)
        for i in indexes:
            if cells[i] is not None:
                cells[i] = base64.b64decode(cells[i], validate=True)
        out.append(cells)
    return out


def ensure(admin: SpannerAdmin, attempts: int = 20, delay: float = 1.5) -> None:
    """Retry `admin.ensure()` until it succeeds or `attempts` is exhausted.

    `services/spanner/compose.yaml` declares NO healthcheck, so `docker compose up
    -d --wait` returns as soon as the container is *running*, not once the emulator's
    gRPC endpoint is actually accepting connections -- this retry IS the readiness
    gate for the whole service, not a defensive nicety.
    """
    last = None
    for _ in range(attempts):
        try:
            admin.ensure()
            return
        except Exception as e:  # noqa: BLE001 -- re-raised after retries
            last = e
            time.sleep(delay)
    raise last


def admins_for(tokens) -> list:
    """One `SpannerAdmin` per dialect, both backed by the single emulator instance,
    built from a `provides:`-token dict (`inttest.tokens.build_tokens`'s output, or the
    `spanner_config` fixture's). Ensures the instance + both databases exist.

    `dbroutes.connection_params("spanner-google"/"spanner-postgres", tokens)` already
    returns exactly `SpannerAdmin`'s dsn shape, so building both admins through it (a
    simplification over the live tier, which assembles the dsn dicts by hand from
    `resolved.base`) means the routes and the admins can't drift apart.
    """
    from inttest.dbroutes import connection_params

    google = SpannerAdmin(connection_params("spanner-google", tokens))
    pg = SpannerAdmin(connection_params("spanner-postgres", tokens))
    ensure(google)  # creates the instance + the GoogleSQL database
    ensure(pg)  # instance already exists; creates the PostgreSQL database
    return [(google, "spanner-google"), (pg, "spanner-postgres")]
