from __future__ import annotations
import os
import re
import json
from livetest.sqlutil import coerce_cell

# Spanner admin client — the Spanner counterpart to PgAdmin/OraAdmin, on the
# google-cloud-spanner client (which targets the emulator when SPANNER_EMULATOR_HOST
# is set). Bound to one database (a dialect); the live tier registers one admin per
# dialect (spanner-google -> GoogleSQL db, spanner-postgres -> PostgreSQL db).
# Used to bootstrap the instance + database, run target-table DDL, and read rows back
# for assertions. Reads the emulator directly from the host (localhost:9010).

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

def _check_table(table: str) -> str:
    parts = table.split(".")
    if not (1 <= len(parts) <= 2) or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"unsafe table name: {table!r}")
    return ".".join(parts)

def _split_ddl(sql: str) -> list[str]:
    # DDL script -> list of statements for update_ddl (no trailing ';', no comments).
    from livetest.sqlutil import split_sql_statements
    return split_sql_statements(sql)   # string-aware ';' split (shared util)

def _rows_to_dicts(field_names, rows) -> list[dict]:
    return [{c: coerce_cell(v) for c, v in zip(field_names, r)} for r in rows]

def _jsonify(v):
    # Normalize one column value from the Spanner client into a plain JSON-comparable value.
    # A JSON column arrives as JsonObject (dict subclass); serialize() yields correct text even
    # for a top-level array, which we json.loads back to a plain dict/list/scalar. NULL JSON
    # serializes to None. A native ARRAY<T> column arrives as a plain list -> pass through.
    if v is None:
        return None
    ser = getattr(v, "serialize", None)
    if callable(ser):
        text = ser()
        return None if text is None else json.loads(text)
    return v

# Cache of google Clients by (project, emulator_host). A Client -- and every Database
# handed out by one -- owns gRPC channels that are never closed, and each channel burns
# fds (on macOS grpc polls, so every completion queue costs a wakeup pipe). Building one
# per call leaks until the process hits EMFILE ("Too many open files"), which surfaces as
# grpc core noise (completion_queue.cc / wakeup_fd_pipe.cc) rather than a Python error.
_CLIENTS: dict = {}

class SpannerAdmin:
    def __init__(self, dsn: dict, database=None, client=None):
        # dsn: {project, instance, database, dialect, emulator_host}
        #   dialect: "google_standard_sql" | "postgresql"
        self.dsn = dsn
        self._database = database   # injectable google Database (query/ddl)
        self._client = client       # injectable google Client (ensure)
        self._db_cache = None       # memoized real Database (see _CLIENTS)

    # --- google client plumbing (skipped when fakes are injected) ---------------
    def _spanner_client(self):
        if self._client is not None:
            return self._client
        emulator = self.dsn["emulator_host"]
        os.environ.setdefault("SPANNER_EMULATOR_HOST", emulator)
        key = (self.dsn["project"], emulator)
        if key not in _CLIENTS:
            from google.cloud import spanner
            _CLIENTS[key] = spanner.Client(project=self.dsn["project"])
        return _CLIENTS[key]

    def _db(self):
        if self._database is not None:
            return self._database
        if self._db_cache is None:
            self._db_cache = (self._spanner_client()
                              .instance(self.dsn["instance"])
                              .database(self.dsn["database"]))
        return self._db_cache

    # --- service bootstrap ------------------------------------------------------
    def ensure(self) -> None:
        # Create the emulator instance (idempotent) and this database with its dialect.
        client = self._spanner_client()
        cfg = f"projects/{self.dsn['project']}/instanceConfigs/emulator-config"
        inst = client.instance(self.dsn["instance"], configuration_name=cfg, display_name="slt")
        if not inst.exists():
            inst.create().result(120)
        from google.cloud.spanner_admin_database_v1 import DatabaseDialect
        dialect = (DatabaseDialect.POSTGRESQL if self.dsn["dialect"] == "postgresql"
                   else DatabaseDialect.GOOGLE_STANDARD_SQL)
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
                self._db_cache = None   # a memoized handle now points at the dropped db
        else:
            db.create().result(120)

    # --- ddl + reads (uniform with PgAdmin/OraAdmin) ----------------------------
    def run_sql(self, sql: str) -> None:
        # Route DDL (CREATE/DROP/ALTER) through update_ddl and DML (INSERT/UPDATE/DELETE)
        # through a read-write transaction (execute_update), so this admin can both build
        # the schema AND seed rows on Spanner (needed now that a Spanner table can be the
        # source, read by SpannerBatchReader).
        ddl, dml = [], []
        for stmt in _split_ddl(sql):
            head = stmt.split(None, 1)[0].upper() if stmt.split() else ""
            (ddl if head in ("CREATE", "DROP", "ALTER") else dml).append(stmt)
        if ddl:
            self._db().update_ddl(ddl).result(120)
        if dml:
            def _txn(txn):
                for s in dml:
                    txn.execute_update(s)
            self._db().run_in_transaction(_txn)

    def drop_test_change_streams(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this test's change streams. Same prefix contract as
        `drop_test_tables`.

        Not optional tidiness -- a change stream is a CAPPED resource. Spanner allows at
        most 3 tracking the same table (or ALL), so a leaked one is not clutter, it is a
        third of the budget gone until the emulator is recreated. The change-stream
        reader cases each ship `CREATE CHANGE STREAM IF NOT EXISTS ${TID}SltCdcReaderStream FOR
        ALL` in their `ddl:`, teardown dropped only TABLES, and three runs' worth of
        leftovers wedged every later run with

          400 ... it is not allowed to have more than 3 Change Streams tracking the
          same table or non-key column or ALL: ALL

        Dropped BEFORE the tables (see the caller): `FOR ALL` does not block a table drop,
        but a stream tracking a table explicitly does, and ordering it this way costs
        nothing. A database that cannot list streams is skipped, but a listed stream whose
        DROP fails raises once every stream has been tried: a leaked stream holds one of the
        three slots, so it must surface as a cleanup failure rather than read as clean."""
        try:
            _, rows = self._query(
                "SELECT change_stream_name FROM information_schema.change_streams "
                "WHERE change_stream_schema IN ('', 'public')")
        except Exception:      # noqa: BLE001 -- best-effort teardown; never mask the real failure
            return
        names = [r[0] for r in rows]
        if prefix:
            low = prefix.lower()
            names = [n for n in names if n.lower().startswith(low)]
        failed = {}
        for name in names:
            if not _IDENT.match(name):
                continue
            try:
                self._db().update_ddl([f"DROP CHANGE STREAM {name}"]).result(120)
            except Exception as e:  # noqa: BLE001 -- the rest are still tried; raised below
                failed[name] = repr(e)
        if failed:
            raise RuntimeError(f"could not drop change stream(s): {failed}")

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop user tables in this admin's database. prefix ""
        drops ALL user tables (serial runs); a non-empty prefix (the per-test ${TID})
        drops only this test's tables so a concurrent sibling's tables survive.
        Filters to the user schema ('' for GoogleSQL, 'public' for the PostgreSQL
        dialect) so information_schema/spanner_sys never match. Multi-pass: an
        interleaved child / FK parent can only drop after its dependents, so retry
        until no progress. Best-effort teardown.

        Drops this test's CHANGE STREAMS first: plugin.py's teardown reaches every admin
        through `drop_test_tables` alone, so hanging the stream cleanup here is what makes
        it run at all -- and streams are a capped resource whose leak is fatal rather than
        untidy (see `drop_test_change_streams`). A failed stream drop is raised after the
        tables have been tried."""
        try:
            self.drop_test_change_streams(prefix)
            stream_error = None
        except RuntimeError as e:
            stream_error = e
        try:
            _, rows = self._query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema IN ('', 'public')")
        except Exception as e:
            if stream_error is not None:
                raise RuntimeError(f"{e!r}; and {stream_error}") from e
            raise
        tables = [r[0] for r in rows]
        if prefix:
            low = prefix.lower()
            tables = [t for t in tables if t.lower().startswith(low)]
        remaining = [t for t in tables if _IDENT.match(t)]
        while remaining:
            failed = []
            for t in remaining:
                try:
                    self._db().update_ddl([f"DROP TABLE {t}"]).result(120)
                except Exception:      # noqa: BLE001 -- dependency ordering; retried below
                    failed.append(t)
            if len(failed) == len(remaining):
                break   # no progress -- leave the rest (best-effort)
            remaining = failed
        if stream_error is not None:
            raise stream_error

    def _query(self, sql: str):
        with self._db().snapshot() as snap:
            rs = snap.execute_sql(sql)
            rows = list(rs)
            cols = [f.name for f in rs.fields]
            return cols, rows

    def count_rows(self, table: str) -> int:
        _, rows = self._query(f"SELECT count(*) FROM {_check_table(table)}")
        return int(rows[0][0])

    def select_rows(self, table: str) -> list[dict]:
        cols, rows = self._query(f"SELECT * FROM {_check_table(table)}")
        return _rows_to_dicts(cols, rows)

    def select_json_rows(self, table: str, key_cols, column: str) -> list[tuple]:
        # Typed read for the `json` assert tier: return [(key_tuple, parsed_value), ...].
        # key_cols/column are whitelisted via _IDENT (interpolated into SQL).
        #
        # Read the raw column value and normalize it here rather than via SQL. A JSON column
        # comes back as google-cloud-spanner's JsonObject, a *dict* subclass that collapses to
        # {} under json.dumps even when it wraps a top-level JSON ARRAY -- so we convert it via
        # JsonObject.serialize() (which honors its internal array/scalar/object/null shape) and
        # json.loads back to a plain dict/list/scalar/None. A native Spanner ARRAY<T> column
        # comes back as a plain Python list and passes through untouched.
        #
        # NB: do NOT wrap the column in TO_JSON_STRING -- the emulator raises UNIMPLEMENTED for
        # TO_JSON_STRING over an ARRAY<T> column ("not supported on values of type ARRAY<...>").
        for c in list(key_cols) + [column]:
            if not _IDENT.match(c):
                raise ValueError(f"unsafe column name: {c!r}")
        keys = ", ".join(key_cols)
        _, rows = self._query(f"SELECT {keys}, {column} FROM {_check_table(table)}")
        n = len(key_cols)
        out = []
        for r in rows:
            key = tuple(str(r[i]) for i in range(n))
            out.append((key, _jsonify(r[n])))
        return out
