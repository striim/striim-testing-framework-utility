from __future__ import annotations
import re
import time
import warnings
from livetest.sqlutil import coerce_cell, split_sql_statements

# Vertica admin for pytest's own connection (setup + DDL/seed + diff/data reads), backed by
# vertica-python. Mirrors TeradataAdmin: one admin drives ONE data role (role="source" ->
# qasource, "target" -> qatarget, "admin" -> the dsn admin user, dbadmin by default); setup
# runs as the admin. Unlike Teradata, a Vertica user and its schema are separate objects:
# each data user owns a same-named schema (services/vertica/images/vertica/init.sql).
#
# dsn keys are services/vertica/service.yaml's: host/port/dbname, admin_user/admin_password,
# source_user/source_password/source_schema, target_user/target_password/target_schema.

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A password safe to inline into CREATE USER (letters/digits/underscore).
_SIMPLE_PW = re.compile(r"^[A-Za-z0-9_]+$")
# First-connect transients: the port is published before the database accepts logins. The
# compose healthcheck gates on a real login, so these mostly matter for a live instance that
# is restarting.
_TRANSIENT = ("connection refused", "connection reset", "timed out", "eof")


def _default_connect(**kw):
    import vertica_python
    # TLS is used when the server offers it ("prefer"); the stock container offers none, and
    # vertica-python then warns on every connection. That warning is expected here.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="TLS is not configured on the server")
        return vertica_python.connect(host=kw["host"], port=int(kw["port"]), user=kw["user"],
                                      password=kw["password"], database=kw["database"],
                                      autocommit=True, connection_timeout=30)


class VerticaAdmin:
    """dsn keys as above; absent role keys fall back to the admin so a minimal dsn still works."""

    def __init__(self, dsn: dict, connect=None, role: str = "source"):
        self.dsn = dsn
        self.role = role
        self._connect = connect or _default_connect

    def _creds(self, which: str):
        admin = (self.dsn.get("admin_user", "dbadmin"), self.dsn.get("admin_password", "striim"))
        if which == "admin":
            return admin
        return (self.dsn.get(f"{which}_user", admin[0]),
                self.dsn.get(f"{which}_password", admin[1]))

    def _schema_of(self, which: str) -> str:
        user, _ = self._creds(which)
        schema = self.dsn.get(f"{which}_schema", user)
        if not _IDENT.match(schema):
            raise ValueError(f"unsafe schema name: {schema!r}")
        return schema

    def _schema(self) -> str:
        return self._schema_of(self.role)

    def _open(self, which: str | None = None, connect_timeout: float = 90.0, poll: float = 3.0):
        user, pw = self._creds(which or self.role)
        deadline = time.monotonic() + connect_timeout
        while True:
            conn = None
            try:
                conn = self._connect(host=self.dsn["host"], port=self.dsn["port"],
                                     database=self.dsn.get("dbname", "sltdb"),
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
                if not any(t in str(e).lower() for t in _TRANSIENT):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(poll)

    def _ready(self, which: str) -> bool:
        """True when ``which`` can log in and its schema exists."""
        try:
            conn = self._open(which=which)
        except Exception:                 # noqa: BLE001 -- missing user, or wrong password
            return False
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM v_catalog.schemata WHERE LOWER(schema_name) = LOWER(:s)",
                        {"s": self._schema_of(which)})
            return bool(cur.fetchall())
        finally:
            conn.close()

    def ensure_setup(self) -> None:
        # Create the qasource/qatarget users and their schemas from the admin if they are
        # missing. The container's init.sql already creates them; this covers a live instance
        # and a container whose init did not finish. Idempotent. A role that can already log
        # in and see its schema needs nothing, so the admin login is used only to create what
        # is missing: a live instance may not hand out admin credentials, and its existing
        # users may have any password.
        missing = [w for w in ("source", "target") if not self._ready(w)]
        if not missing:
            return
        admin, _ = self._creds("admin")
        conn = self._open(which="admin")
        try:
            cur = conn.cursor()
            for which in missing:
                user, pw = self._creds(which)
                schema = self._schema_of(which)
                cur.execute("SELECT 1 FROM v_catalog.users WHERE LOWER(user_name) = LOWER(:u)", {"u": user})
                if not cur.fetchall():
                    # The checks guard the inlined CREATE USER below.
                    if not (_IDENT.match(user) and _SIMPLE_PW.match(pw)):
                        raise ValueError(f"cannot create {which} user {user!r}: unsafe "
                                         f"user name or password for inlined DDL")
                    # CREATE USER is DDL — the password cannot be bound as a parameter, so
                    # inline the (validated, letters/digits/underscore) value.
                    cur.execute(f"CREATE USER {user} IDENTIFIED BY '{pw}'")
                elif not _IDENT.match(user):
                    raise ValueError(f"unsafe {which} user name: {user!r}")
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {user}")
                cur.execute(f"ALTER USER {user} SEARCH_PATH {schema}, public")
        finally:
            conn.close()

    def run_sql(self, sql: str) -> None:
        schema = self._schema()
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(f"SET SEARCH_PATH TO {schema}, public")
            for stmt in split_sql_statements(sql):
                cur.execute(stmt)
        finally:
            conn.close()

    def _list_tables(self, prefix: str = "") -> list[str]:
        """This role's tables in its schema, optionally those whose name starts with
        ``prefix``. Vertica names are case-insensitive, so match that way, in Python: the
        tid ends in '_', which LIKE would read as a wildcard (as MySQLAdmin notes)."""
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute("SELECT table_name FROM v_catalog.tables WHERE LOWER(table_schema) = LOWER(:s)",
                        {"s": self._schema()})
            tables = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        if prefix:
            low = prefix.lower()
            tables = [t for t in tables if t.lower().startswith(low)]
        return tables

    def _drop(self, tables: list[str]) -> None:
        # CASCADE drops the projections and the foreign keys that reference a table, so the
        # order of the drops does not matter. Names that are not plain identifiers are
        # skipped, as MySQLAdmin._drop does: this runs at teardown, and one odd name must not
        # cost the whole cleanup.
        schema = self._schema()
        drops = [f"DROP TABLE IF EXISTS {schema}.{t} CASCADE" for t in tables if _IDENT.match(t)]
        if not drops:
            return
        conn = self._open()
        try:
            cur = conn.cursor()
            for stmt in drops:
                cur.execute(stmt)
        finally:
            conn.close()

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this role's tables in its fixed schema (qasource or
        qatarget). prefix "" drops ALL of them -- serial runs, mirroring the Postgres
        whole-schema reset; a non-empty prefix (the per-test ${TID}) drops only this test's
        tables so a concurrent sibling's survive. plugin.teardown_ddl_tables finds it by
        name."""
        if self.role == "admin":
            return    # the admin route owns no test tables; never sweep the admin's schema
        if prefix and not _IDENT.match(prefix):
            raise ValueError(f"unsafe identifier: {prefix!r}")
        self._drop(self._list_tables(prefix))

    def _qualified(self, table: str) -> str:
        # Accept schema.table or bare table (-> this role's schema). Emit bare validated
        # identifiers: unquoted Vertica names resolve case-insensitively.
        parts = table.split(".")
        if len(parts) == 1:
            parts = [self._schema(), parts[0]]
        if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
            raise ValueError(f"table must be schema.table with safe identifiers: {table!r}")
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
        # Vertica's default isolation is READ COMMITTED, so this count never includes a
        # writer's uncommitted rows and no count_rows_committed is needed (assertions/data.py).
        _, rows = self._query(f"SELECT COUNT(*) FROM {self._qualified(table)}")
        return int(rows[0][0])

    def select_rows(self, table: str) -> list[dict]:
        cols, rows = self._query(f"SELECT * FROM {self._qualified(table)}")
        return [{c: coerce_cell(v) for c, v in zip(cols, r)} for r in rows]
