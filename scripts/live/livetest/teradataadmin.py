from __future__ import annotations
import re
import time
from livetest.sqlutil import coerce_cell, split_sql_statements

# Teradata admin for pytest's own connection (setup + DDL/seed + diff/data reads), backed
# by teradatasql. The framework ships no Teradata service: this
# serves a customer's own instance, or a consumer service named `teradata` from
# servicesRoots. Mirrors MssqlAdmin: one admin drives ONE data role (role="source" ->
# qasource, "target" -> qatarget, "admin" -> the explicitly configured admin user, for a seed
# a data user may not run, such as SYSLIB.AbortSessions); setup runs as the configured admin. In Teradata a user is
# also a database, so each role's schema is its user's database.

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
# A password safe to inline into CREATE USER (letters/digits/underscore).
_SIMPLE_PW = re.compile(r"^[A-Za-z0-9_]+$")
# Space given to a data user that ensure_setup creates; matches images/teradata/bake.sql.
_PERM = "10e9"
# First-connect transients: the VM's port forward accepts before the guest listens, and
# the database refuses logons until startup finishes. The compose healthcheck gates on a
# real login, so these mostly matter for a live instance that is restarting.
_TRANSIENT = ("connection refused", "connection reset", "EOF", "i/o timeout",
              "Logons are only enabled", "DBS is not accepting")


def _default_connect(**kw):
    import teradatasql
    # logon_timeout (s) and connect_timeout (ms) bound a logon that never answers: the
    # driver's default is to wait forever, and _open's deadline only covers failures.
    return teradatasql.connect(host=kw["host"], dbs_port=str(kw["port"]),
                               user=kw["user"], password=kw["password"],
                               logon_timeout="60", connect_timeout="30000")


class TeradataAdmin:
    """dsn keys: host/port, user/password (the admin), source_user/source_password/
    source_schema, target_user/target_password/target_schema — absent role keys fall back
    to the admin so a minimal dsn still works."""

    def __init__(self, dsn: dict, connect=None, role: str = "source"):
        self.dsn = dsn
        self.role = role
        self._connect = connect or _default_connect

    def _creds(self, which: str):
        if which == "admin":
            return self.dsn["user"], self.dsn["password"]
        return (self.dsn.get(f"{which}_user", self.dsn["user"]),
                self.dsn.get(f"{which}_password", self.dsn["password"]))

    def _schema(self) -> str:
        user, _ = self._creds(self.role)
        schema = self.dsn.get(f"{self.role}_schema", user)
        if not _IDENT.match(schema):
            raise ValueError(f"unsafe schema name: {schema!r}")
        return schema

    def _open(self, which: str | None = None, connect_timeout: float = 90.0, poll: float = 3.0):
        user, pw = self._creds(which or self.role)
        deadline = time.monotonic() + connect_timeout
        while True:
            conn = None
            try:
                conn = self._connect(host=self.dsn["host"], port=self.dsn["port"],
                                     user=user, password=pw)
                cur = conn.cursor()
                cur.execute("SELECT 1")           # probe: database accepting queries
                cur.fetchall()
                return conn
            except Exception as e:                # noqa: BLE001 — re-raised below
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not any(t.lower() in str(e).lower() for t in _TRANSIENT):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(poll)

    def _can_log_in(self, which: str) -> bool:
        try:
            self._open(which=which).close()
            return True
        except Exception:                 # noqa: BLE001 -- missing user, or wrong password
            return False

    def ensure_setup(self) -> None:
        # Create the qasource/qatarget users (each also its own database) using the configured admin if they
        # are missing. The Docker disks already carry them (bake.sql); this covers a live
        # instance. Idempotent. A role that can already log in needs nothing, so the admin
        # login is used only to create a missing user: a live instance may not hand out
        # admin credentials, and its existing users may have any password.
        missing = [w for w in ("source", "target") if not self._can_log_in(w)]
        if not missing:
            return
        admin = self.dsn["user"]
        conn = self._open(which="admin")
        try:
            cur = conn.cursor()
            for which in missing:
                user, pw = self._creds(which)
                cur.execute("SELECT 1 FROM DBC.DatabasesV WHERE DatabaseName = ?", [user])
                if not cur.fetchall():
                    # The checks guard the inlined CREATE USER below.
                    if not (_IDENT.match(user) and _IDENT.match(admin) and _SIMPLE_PW.match(pw)):
                        raise ValueError(f"cannot create {which} user {user!r}: unsafe "
                                         f"user/admin name or password for inlined DDL")
                    # CREATE USER is DDL — the password cannot be bound as a parameter, so
                    # inline the (validated, letters/digits/underscore) values. Quoted, since
                    # an unquoted password may not start with a digit.
                    cur.execute(f"CREATE USER {user} FROM {admin} AS "
                                f'PERM = {_PERM}, SPOOL = {_PERM}, PASSWORD = "{pw}"')
        finally:
            conn.close()

    def run_sql(self, sql: str) -> None:
        conn = self._open()
        try:
            cur = conn.cursor()
            for stmt in split_sql_statements(sql):
                try:
                    cur.execute(stmt)
                except Exception as e:
                    # ⚠ Teradata has NO `DROP TABLE IF EXISTS`. Every DDL file here opens with
                    # DROPs so a case can be re-run, and on a FRESH instance those objects do
                    # not exist yet -- Error 3807 -- which killed the whole DDL step before it
                    # created anything. The failure then surfaced far downstream as "Object
                    # ... does not exist" from the ASSERTION, naming neither the DROP nor the
                    # DDL file. Found on the first run against a rebuilt slt-teradata; every
                    # Teradata arm had been relying on the container's persisted tables.
                    #
                    # Narrow on purpose, and identical to the integration tier's rule
                    # (scripts/integration/inttest/dbroutes.py): only 3807, and only for a
                    # DROP. A 3807 from anything else is a real missing object and must still
                    # fail, or a case could assert against a table that was never created.
                    # ⚠ "[Error 3807]" not "3807": the driver formats an error as
                    # "[Session %v] [Teradata Database] [Error %v] [SQLState %v] %v", so the
                    # SESSION number is in the message -- a DROP failing with any other error
                    # in session 3807 would be silently skipped. The trailing text names the
                    # object too, and ${TID} is hex, where 3807 is a legal substring.
                    if "[Error 3807]" in str(e) and stmt.lstrip().upper().startswith("DROP"):
                        continue
                    raise
        finally:
            conn.close()

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this role's tables in its fixed database
        (qasource/qatarget). prefix "" drops ALL of them -- serial runs, mirroring the
        Postgres whole-schema reset; a non-empty prefix (the per-test ${TID}) drops only
        this test's tables so a concurrent sibling's tables survive (Teradata names are
        case-insensitive, so match that way). Multi-pass: a table referenced by another's
        FK can only drop after its dependents. Raises naming any table that will not drop."""
        if self.role == "admin":
            return    # the admin route owns no test tables; never sweep dbc
        schema = self._schema()
        conn = self._open()
        try:
            cur = conn.cursor()
            # T = table with a primary index, O = no-primary-index table.
            cur.execute("SELECT TRIM(TableName) FROM DBC.TablesV "
                        "WHERE DatabaseName = ? AND TableKind IN ('T', 'O')", [schema])
            tables = [r[0] for r in cur.fetchall()]
            if prefix:
                low = prefix.lower()
                tables = [t for t in tables if t.lower().startswith(low)]
            remaining = [t for t in tables if _IDENT.match(t)]
            while remaining:
                failed = []
                for t in remaining:
                    try:
                        cur.execute(f"DROP TABLE {schema}.{t}")
                    except Exception:      # noqa: BLE001 -- FK ordering; retried below
                        failed.append(t)
                if len(failed) == len(remaining):
                    # No progress. Raise rather than return quietly: teardown_ddl_tables turns
                    # this into a visible warning, and a table left behind otherwise surfaces
                    # only as the next run's "already exists".
                    raise RuntimeError(f"could not drop {schema} tables: {', '.join(failed)}")
                remaining = failed
        finally:
            conn.close()

    def _qualified(self, table: str) -> str:
        # Accept db.table or bare table (-> this role's database). Emit bare validated
        # identifiers: unquoted Teradata names resolve case-insensitively.
        parts = table.split(".")
        if len(parts) == 1:
            parts = [self._schema(), parts[0]]
        if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
            raise ValueError(f"table must be db.table with safe identifiers: {table!r}")
        return ".".join(parts)

    def _query(self, sql: str):
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            return cols, cur.fetchall()
        finally:
            conn.close()

    def count_rows(self, table: str) -> int:
        # Polled while a Striim writer is loading the table. A plain SELECT takes a READ lock
        # that queues behind the writer's WRITE lock (and can deadlock with it), so read with
        # an ACCESS lock: the count only has to say whether the number is still moving.
        t = self._qualified(table)
        _, rows = self._query(f"LOCKING TABLE {t} FOR ACCESS SELECT COUNT(*) FROM {t}")
        return int(rows[0][0])

    def count_rows_committed(self, table: str) -> int:
        # The ACCESS count above can include rows a writer has not committed; an exact `rows:`
        # assertion confirms with this (assertions/data.py _committed). NOWAIT fails at once if
        # the writer still holds the table, which the poll loop treats as "not yet".
        t = self._qualified(table)
        _, rows = self._query(f"LOCKING TABLE {t} FOR READ NOWAIT SELECT COUNT(*) FROM {t}")
        return int(rows[0][0])

    def select_rows(self, table: str) -> list[dict]:
        # A plain (committed) read, unlike count_rows: match/diff assertions have no separate
        # committed check, so reading with ACCESS could pass them on rows a writer later rolls
        # back. The cost is that a poll waits for the writer's commit, as MssqlAdmin's does.
        cols, rows = self._query(f"SELECT * FROM {self._qualified(table)}")
        return [{c: coerce_cell(v) for c, v in zip(cols, r)} for r in rows]
