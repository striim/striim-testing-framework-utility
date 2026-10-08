from __future__ import annotations
import re
import time
from livetest.sqlutil import coerce_cell

# Oracle service registration can lag container health on a cold start, so the first
# connection may transiently fail with these codes before FREEPDB1 is usable. Retry
# for a bounded window. ORA-01109 (database not open): gvenzl opens FREEPDB1 shortly
# AFTER "DATABASE IS READY" — a connection can succeed against the still-MOUNTED PDB
# and only ops raise it, so _open probes with SELECT 1 and retries on it too.
_TRANSIENT = ("DPY-6001", "DPY-6005", "ORA-12514", "ORA-12541", "ORA-01033", "ORA-01034", "ORA-01109")

# Oracle admin client — the Oracle counterpart to PgAdmin (livetest/pgclient.py).
# Used by the live tier to run an example's Oracle DDL, seed the Oracle source
# (post-start, for CDC), and read Oracle target tables (QATARGET.*) for assertions.
# Uses python-oracledb in THIN mode (pure Python, no Oracle Instant Client needed).
#
# Oracle has no per-test schema isolation here (the Oracle service uses fixed
# schemas — QASOURCE/QATARGET), so there is no create_schema/drop_schema: OP tests
# run serially and each example's DDL drops+recreates its own tables.

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")

def _default_connect(**kw):
    import oracledb
    return oracledb.connect(
        user=kw["user"], password=kw["password"],
        dsn=f'{kw["host"]}:{int(kw["port"])}/{kw["service"]}',
    )

def _check(ident: str) -> str:
    if not _IDENT.match(ident):
        raise ValueError(f"unsafe identifier: {ident!r}")
    return ident

def _split_statements(sql: str) -> list[str]:
    # Split a plain-SQL script into individual statements for python-oracledb,
    # which executes ONE statement per execute() and rejects a trailing ';'.
    # Strips '-- ' line comments and blank/terminator-only chunks. Framework
    # Oracle DDL/seed files must be plain SQL (no sqlplus-only directives, no
    # PL/SQL blocks with internal ';').
    from livetest.sqlutil import split_sql_statements
    out = []
    for s in split_sql_statements(sql):          # string-aware ';' split (no ';'-in-literal footgun)
        s = s.strip().rstrip("/").strip()        # oracledb rejects a trailing '/'
        if s:
            out.append(s)
    return out

class OraAdmin:
    def __init__(self, dsn: dict, connect=None):
        # dsn: {host, port, service, user, password}
        self.dsn = dsn
        self._connect = connect or _default_connect

    def _open(self, connect_timeout: float = 90.0, poll: float = 3.0):
        kw = dict(host=self.dsn["host"], port=int(self.dsn["port"]),
                  service=self.dsn["service"], user=self.dsn["source_user"],
                  password=self.dsn["source_password"])
        deadline = time.monotonic() + connect_timeout
        while True:
            conn = None
            try:
                conn = self._connect(**kw)
                # Probe: a connection can succeed while FREEPDB1 is still MOUNTED
                # (ORA-01109 on ops). Verify the DB is actually open before returning.
                cur = conn.cursor()
                try:
                    cur.execute("SELECT 1 FROM dual")
                finally:
                    cur.close()
                return conn
            except Exception as e:               # noqa: BLE001 — re-raised below
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not any(code in str(e) for code in _TRANSIENT):
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(poll)

    def run_sql(self, sql: str) -> None:
        conn = self._open()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                for stmt in _split_statements(sql):
                    cur.execute(stmt)
            finally:
                cur.close()
        finally:
            conn.close()

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop tables owned by this admin's data user (its fixed
        QASOURCE/QATARGET schema). prefix "" drops ALL of the user's tables -- serial
        runs, mirroring the Postgres whole-schema reset; a non-empty prefix (the
        per-test ${TID_ORACLE}) drops only this test's tables so a concurrent
        sibling's tables survive. Individual drop failures are swallowed (best-effort
        teardown); CASCADE CONSTRAINTS handles cross-table FKs, PURGE skips the
        recycle bin."""
        _, rows = self._query("SELECT table_name FROM user_tables")
        tables = [r[0] for r in rows]
        if prefix:
            up = prefix.upper()
            tables = [t for t in tables if t.upper().startswith(up)]
        conn = self._open()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                for t in tables:
                    if not _IDENT.match(t):
                        continue   # never interpolate an odd name into DDL
                    try:
                        cur.execute(f"DROP TABLE {t} CASCADE CONSTRAINTS PURGE")
                    except Exception:      # noqa: BLE001 -- best-effort teardown
                        pass
            finally:
                cur.close()
        finally:
            conn.close()

    def _qualified(self, table: str) -> str:
        parts = table.split(".")
        if len(parts) != 2:
            raise ValueError(f"table must be schema.table: {table!r}")
        # Emit bare (validated) identifiers: objects created unquoted are stored
        # upper-case and resolve unquoted, so we do not double-quote (which would
        # force case-sensitive matching).
        return ".".join(_check(p) for p in parts)

    def _query(self, sql: str):
        conn = self._open()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                cols = [d[0] for d in (cur.description or [])]
                return cols, cur.fetchall()
            finally:
                cur.close()
        finally:
            conn.close()

    def count_rows(self, table: str) -> int:
        _, rows = self._query(f"SELECT count(*) FROM {self._qualified(table)}")
        return int(rows[0][0])

    def select_rows(self, table: str) -> list[dict]:
        cols, rows = self._query(f"SELECT * FROM {self._qualified(table)}")
        out = []
        for r in rows:
            out.append({c: coerce_cell(v) for c, v in zip(cols, r)})
        return out
