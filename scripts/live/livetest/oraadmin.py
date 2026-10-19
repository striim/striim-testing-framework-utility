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
_SQL_IDENT = r'(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$#]*)'
_OBSERVER_DDL = re.compile(
    r'\s*(?P<operation>CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:NO)?FORCE\s+)?'
    r'(?:(?:EDITIONABLE\s+(?:EDITIONING\s+)?|NONEDITIONABLE\s+|EDITIONING\s+))?'
    r'|DROP\s+|ALTER\s+)(?P<kind>VIEW|TRIGGER)\s+(?:IF\s+EXISTS\s+)?'
    rf'(?P<name>{_SQL_IDENT}(?:\s*\.\s*{_SQL_IDENT})?)', re.I | re.S)


def _default_connect(**kw):
    import oracledb
    return oracledb.connect(
        user=kw["user"], password=kw["password"], program="striim-test-observer",
        dsn=f'{kw["host"]}:{int(kw["port"])}/{kw["service"]}',
    )

def _check(ident: str) -> str:
    if not _IDENT.match(ident):
        raise ValueError(f"unsafe identifier: {ident!r}")
    return ident

def _split_statements(sql: str) -> list[str]:
    # Oracle trigger bodies retain internal semicolons and require a slash-only
    # terminator. Keep this dialect handling local; other engines use plain SQL.
    out, buf = [], []
    quote = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    buf.append(sql[i + 1]); i += 1
                else:
                    quote = ""
            i += 1
            continue
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end
            buf.append("\n")
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("oracle-sql-comment: unterminated block comment")
            buf.append(" "); i = end + 2
            continue
        if ch == "/":
            line_start = sql.rfind("\n", 0, i) + 1
            line_end = sql.find("\n", i)
            if line_end < 0: line_end = len(sql)
            if not sql[line_start:i].strip() and not sql[i + 1:line_end].strip():
                statement = "".join(buf).strip()
                if statement: out.append(statement)
                buf = []; i = line_end + 1
                continue
        if ch in ("'", '"'):
            if ch == "'" and buf and buf[-1] in ("q", "Q"):
                raise ValueError("oracle-sql-quoting: alternative quoting is unsupported; use doubled quotes")
            quote = ch
        if ch == ";" and not re.match(
                r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?TRIGGER\b",
                "".join(buf), re.I):
            statement = "".join(buf).strip()
            if statement: out.append(statement)
            buf = []
        else:
            buf.append(ch)
        i += 1
    statement = "".join(buf).strip()
    if quote:
        raise ValueError("oracle-sql-quoting: unterminated quoted text")
    if re.match(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?TRIGGER\b", statement, re.I):
        raise ValueError("oracle-plsql-terminator: a trigger requires a slash-only line")
    if statement: out.append(statement)
    return out

class OraAdmin:
    def __init__(self, dsn: dict, connect=None, *, prefix=None):
        # dsn: {host, port, service, user, password}
        self.dsn = dsn
        self._connect = connect or _default_connect
        self.prefix, self.touched = prefix, False
        self.observer_prefix = prefix

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
        statements = _split_statements(sql)
        # The privileged route accepts only observer creates/drops. Ordinary
        # data-user SQL keeps its existing contract. Observer objects must be owned
        # by the connected user, so cleanup can find them in USER_OBJECTS.
        for stmt in statements:
            if self.prefix is not None and re.match(
                    r'\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?|ALTER\s+|DROP\s+)MATERIALIZED\s+VIEW\b', stmt, re.I):
                raise ValueError("oracle-observer-ddl-unsupported: materialized views are unsupported on oracle-admin")
            match = _OBSERVER_DDL.match(stmt)
            if self.prefix is not None and (not match or match["operation"].upper().startswith("ALTER")):
                raise ValueError("oracle-observer-ddl-unsupported: oracle-admin accepts only view/trigger CREATE and DROP")
            if match and self.prefix is not None:
                parts = [p[1:-1].replace('""', '"') if p.startswith('"') else p
                         for p in re.findall(_SQL_IDENT, match["name"])]
                if (not self.prefix or not _IDENT.fullmatch(self.prefix)
                        or any(not _IDENT.fullmatch(part) for part in parts)
                        or not parts[-1].upper().startswith(self.prefix.upper())
                        or (len(parts) == 2 and parts[0].upper() != self.dsn["source_user"].upper())):
                    raise ValueError("oracle-observer-ddl-scope: view/trigger must belong to this case and connected user")
        # Ordinary data-user tables/seeds and unprefixed views do not acquire
        # observer teardown obligations. The privileged route is observer-only.
        self.touched = self.touched or self.prefix is not None or any(
            match and self.observer_prefix and
            re.findall(_SQL_IDENT, match["name"])[-1].strip('"').upper().startswith(self.observer_prefix.upper())
            for match in (_OBSERVER_DDL.match(stmt) for stmt in statements))
        conn = self._open()
        try:
            conn.autocommit = True
            cur = conn.cursor()
            try:
                for stmt in statements:
                    match = _OBSERVER_DDL.match(stmt)
                    trigger_entry = None
                    if match and match["kind"].upper() == "TRIGGER" and match["operation"].upper().startswith("CREATE"):
                        from livetest.oracle_ownership import before_create
                        trigger_entry = before_create(self, match["name"])
                    cur.execute(stmt)
                    if trigger_entry is not None:
                        self.observer_ledger._set(trigger_entry, "confirmed")
                    warning = getattr(cur, "warning", None)
                    if (warning is not None and self.prefix is not None and match
                            and match["operation"].upper().startswith("CREATE")):
                        # Compilation warnings leave an INVALID object behind. Remove it
                        # before a database logon trigger can affect another session.
                        match = _OBSERVER_DDL.match(stmt)
                        drop_error = None
                        if match and match["operation"].upper().startswith("CREATE"):
                            parts = re.findall(_SQL_IDENT, match["name"])
                            name = ".".join(part if part.startswith('"') else '"' + part.upper() + '"'
                                            for part in parts)
                            try:
                                cur.execute(f'DROP {match["kind"].upper()} {name}')
                            except Exception as exc:
                                drop_error = str(exc)
                        raise RuntimeError("oracle-ddl-compilation-error: " + str(warning)
                                           + (f"; immediate drop failed: {drop_error}" if drop_error else ""))
            finally:
                cur.close()
        finally:
            conn.close()

    def drop_observer_objects(self, prefix: str) -> None:
        if not prefix or not _IDENT.fullmatch(prefix) or not prefix.endswith("_"):
            raise ValueError("oracle-observer-cleanup-scope: a nonempty case prefix is required")
        query = "SELECT object_name, object_type FROM user_objects WHERE object_type IN ('TRIGGER', 'VIEW')"
        def owned():
            _, rows = self._query(query)
            return {(name, kind) for name, kind in rows if kind in ("TRIGGER", "VIEW")
                    and isinstance(name, str) and name.upper().startswith(prefix.upper())
                    and _IDENT.fullmatch(name)}
        objects = owned()
        from livetest.oracle_ownership import authorise_catalog_drop
        objects, errors = authorise_catalog_drop(self, objects)
        if objects:
            conn = self._open()
            try:
                conn.autocommit = True
                cur = conn.cursor()
                try:
                    for name, kind in sorted(objects, key=lambda item: (item[1] != "TRIGGER", item[0])):
                        try:
                            cur.execute(f'DROP {kind} "{name}"')
                        except Exception as exc:
                            errors.append(f"{kind} {name}: {exc}")
                finally:
                    cur.close()
            finally:
                conn.close()
        remaining = owned()
        if remaining: errors.append(f"objects remain: {sorted(remaining)}")
        if errors:
            raise RuntimeError("oracle-observer-cleanup-failed: " + "; ".join(errors))

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
