from __future__ import annotations
import re
import time
from livetest.sqlutil import coerce_cell as _coerce

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A role password safe to inline into CREATE ROLE (letters/digits/underscore).
_SIMPLE_PW = re.compile(r"^[A-Za-z0-9_]+$")
# runident.derive emits slt_ + t + nine lowercase hexadecimal digits.
_HARNESS_SLOT_PATTERN = r"^slt_t[0-9a-f]{9}$"

# Row-value coercion is the shared coerce_cell (livetest/sqlutil): psycopg2 auto-parses a
# json/jsonb column into a Python dict/list -> canonical JSON text (sorted keys, compact)
# so a jsonb target compares key-order-independently; bool -> "true"/"false"; bytea ->
# hex; None stays None; everything else str-coerces.

def _default_connect(**kw):
    import psycopg2
    return psycopg2.connect(**kw)

def _is_lock_timeout(e: Exception) -> bool:
    # psycopg2 raises errors.LockNotAvailable (SQLSTATE 55P03) for lock_timeout; match on the
    # SQLSTATE so a fake or a different driver build classifies the same way.
    return getattr(e, "pgcode", None) == "55P03" or "canceling statement due to lock timeout" in str(e)

def _check(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"unsafe schema name: {name!r}")
    return name

class PgAdmin:
    """Postgres admin for pytest's own connection (setup + DDL/seed + diff reads).

    Consistent with every other DB, a test's Postgres carries an ADMIN (the superuser,
    ``admin_user``) plus two data roles — ``qasource`` (source) and ``qatarget`` (target) —
    each owning a same-named schema. One PgAdmin instance drives ONE data role, chosen by
    ``role`` ("source" -> qasource, "target" -> qatarget): its ``run_sql``/reads connect as
    that role and operate in that role's schema. Setup and the per-test wipe use the admin
    connection. A source table T and a same-named target table T thus coexist as
    ``qasource.T`` / ``qatarget.T``.

    The dsn carries ``admin_user``/``admin_password``, ``source_user``/``source_password``/
    ``source_schema``, ``target_user``/``target_password``/``target_schema``. Missing keys
    fall back to the ``source_*`` values so a minimal single-user dsn still works.
    """

    def __init__(self, dsn: dict, connect=None, role: str = "source"):
        self.dsn = dsn
        self._connect = connect or _default_connect
        self.role = role
        #: One dict (pid, usename, application_name, state, idle_s, xact_s, query) per backend
        #: a blocked DDL terminated (design §4.4), in order. Callers report and may clear it.
        self.terminated: list[dict] = []

    def _creds(self, which: str):
        # which: "admin" | "source" | "target". Fall back to source_* for any absent key.
        user = self.dsn.get(f"{which}_user", self.dsn.get("source_user"))
        pw = self.dsn.get(f"{which}_password", self.dsn.get("source_password"))
        return user, pw

    def _schema(self) -> str:
        default = "qasource" if self.role == "source" else "qatarget"
        return _check(self.dsn.get(f"{self.role}_schema", default))

    def _open(self, which: str | None = None, attempts: int = 30, delay: float = 2.0):
        # Retry the connect through Postgres cold-start transients: the container's
        # healthcheck can report ready a beat before the very first client connection
        # succeeds. `which` selects the credential set (defaults to this instance's role).
        user, password = self._creds(which or self.role)
        last = None
        for _ in range(attempts):
            try:
                conn = self._connect(
                    host=self.dsn["host"], port=int(self.dsn["port"]),
                    dbname=self.dsn["dbname"], user=user, password=password,
                )
                conn.autocommit = True
                return conn
            except Exception as e:      # psycopg2.OperationalError on a cold/refused socket
                last = e
                time.sleep(delay)
        raise last

    def _exec(self, statements: list[str], which: str | None = None,
              lock_timeout_ms: int | None = None) -> None:
        if lock_timeout_ms:
            self._exec_ddl(statements, which, lock_timeout_ms)
            return
        conn = self._open(which)
        try:
            cur = conn.cursor()
            try:
                for s in statements:
                    cur.execute(s)
            finally:
                cur.close()
        finally:
            conn.close()

    # DDL that can block on another session's lock waits this long before failing with a
    # message naming the blockers (design §4.4). While it waits, idle-in-transaction blockers
    # are terminated (see _exec_ddl), so reaching the timeout means an ACTIVE session holds
    # the lock -- a running test's writer, most likely -- which the harness must not kill.
    DDL_LOCK_TIMEOUT_MS = 60_000
    # How often a waiting DDL looks for blockers.
    _BLOCKER_POLL_S = 1.0

    def _exec_ddl(self, statements: list[str], which: str | None, lock_timeout_ms: int) -> None:
        """Runs `statements` with a lock timeout, and while any of them waits on a lock,
        terminates the backends blocking it that sit ``idle in transaction`` -- doing nothing,
        waiting on their client, holding the lock (design §4.4: a DatabaseWriter under
        ``CommitPolicy ... Interval:0`` whose trailing batch never reached the count, 20m47s
        idle, the next run's DROP SCHEMA blocked 8m57s behind it). Exact, not heuristic:
        ``pg_blocking_pids()`` names what THIS statement is waiting for, so a sibling worker's
        writer idle on its own tables is never on the list, and a blocker that is ACTIVE is
        left alone to run the statement into its timeout and a message naming it.
        Terminated backends accumulate on ``self.terminated`` for the caller to report."""
        import threading
        conn = self._open(which)
        try:
            cur = conn.cursor()
            try:
                cur.execute(f"SET lock_timeout = '{int(lock_timeout_ms)}ms'")
                cur.execute("SELECT pg_backend_pid()")
                pid_rows = cur.fetchall() or []
                ddl_pid = pid_rows[0][0] if pid_rows else None    # None: no watcher, timeout only
                for stmt in statements:
                    failure: list[BaseException] = []

                    def _run(stmt=stmt):
                        try:
                            cur.execute(stmt)
                        except BaseException as e:      # handed back on the caller's thread
                            failure.append(e)

                    t = threading.Thread(target=_run, name="slt-pg-ddl", daemon=True)
                    t.start()
                    t.join(self._BLOCKER_POLL_S)
                    while t.is_alive():
                        if ddl_pid is not None:
                            self._terminate_idle_blockers_of(ddl_pid)
                        t.join(self._BLOCKER_POLL_S)
                    if failure:
                        e = failure[0]
                        if _is_lock_timeout(e):
                            raise RuntimeError(self._lock_timeout_message(stmt, lock_timeout_ms)) from e
                        raise e
            finally:
                cur.close()
        finally:
            conn.close()

    def _terminate_idle_blockers_of(self, ddl_pid: int) -> list[dict]:
        keys = ("pid", "usename", "application_name", "state", "idle_s", "xact_s", "query")
        try:
            _, rows = self._query(
                "SELECT a.pid, a.usename, a.application_name, a.state, "
                "EXTRACT(EPOCH FROM (now() - a.state_change))::int, "
                "EXTRACT(EPOCH FROM (now() - a.xact_start))::int, left(a.query, 120) "
                "FROM pg_stat_activity a WHERE a.pid = ANY(pg_blocking_pids(%s)) "
                "AND a.state LIKE 'idle in transaction%%' ORDER BY a.pid",
                params=(ddl_pid,), which="admin")
        except Exception:           # the watcher must never fail the DDL it is helping
            return []
        found = [dict(zip(keys, r)) for r in rows]
        for row in found:
            try:
                self._query("SELECT pg_terminate_backend(%s)", params=(row["pid"],), which="admin")
                self.terminated.append(row)
            except Exception:       # gone already, or not ours to kill -- the DDL will tell
                pass
        return found

    def _lock_timeout_message(self, statement: str, waited_ms: int) -> str:
        try:
            _, rows = self._query(
                "SELECT pid, usename, application_name, state, "
                "EXTRACT(EPOCH FROM (now() - xact_start))::int, left(query, 120) "
                "FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
                "AND xact_start IS NOT NULL ORDER BY xact_start",
                params=(self.dsn["dbname"],), which="admin")
            holders = "; ".join(
                f"pid {pid} {user} {app!r} {state} xact {age}s: {q!r}" for pid, user, app, state, age, q in rows)
        except Exception as e:      # the diagnostic must never mask the timeout itself
            holders = f"(could not list sessions: {e})"
        return (f"postgres DDL waited {waited_ms / 1000:.0f}s for a lock and gave up: {statement!r}. "
                f"Sessions with an open transaction: {holders or 'none'}. A holder that is ACTIVE "
                f"(mid-statement) is never terminated by the harness -- it is a running app's "
                f"writer or a kept app: stop it. Idle-in-transaction blockers are terminated "
                f"automatically (design §4.4).")

    def _role_password(self, which: str) -> str:
        _, pw = self._creds(which)
        if not _SIMPLE_PW.match(pw or ""):
            raise ValueError(f"unsafe {which} role password for CREATE ROLE: {pw!r}")
        return pw

    def ensure_setup(self) -> None:
        # Create the qasource/qatarget login roles + their owned schemas, idempotently, as
        # the admin (superuser). Postgres has no CREATE ROLE IF NOT EXISTS, and an
        # IF NOT EXISTS(...) guard is a TOCTOU race under concurrent workers (two see "absent"
        # then both CREATE -> "role already exists"). Instead CREATE unconditionally and
        # swallow duplicate_object in the DO block — atomic and concurrency-safe. Under xdist
        # the caller also runs this exactly once via services.ensure_provisioned_once, so this
        # is defense-in-depth. The (validated) password is inlined because it can't be a bind param.
        stmts = []
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            pw = self._role_password(which)
            schema = _check(self.dsn.get(f"{which}_schema", role))
            # REPLICATION so the PostgreSQLReader (logical decoding) role can create +
            # attach to a wal2json slot — the old single superuser had this implicitly.
            stmts.append(
                f"DO $$ BEGIN CREATE ROLE {role} LOGIN REPLICATION PASSWORD '{pw}'; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$")
            stmts.append(f'GRANT CONNECT ON DATABASE "{_check(self.dsn["dbname"])}" TO {role}')
            stmts.append(f'CREATE SCHEMA IF NOT EXISTS "{schema}" AUTHORIZATION {role}')
        # DatabaseWriter's default checkpoint table, in the shape it auto-creates. Parallel workers
        # share qatarget, and two apps auto-creating it at the same START race: the loser fails on
        # pg_type's unique index and halts with "Checkpoint table {chkpoint} could not be located".
        # Serial runs drop it in reset_schemas; a single writer re-creates it without a race.
        # ALTER ... OWNER takes ACCESS EXCLUSIVE even when nothing changes, so it runs only when the
        # owner is wrong, and both statements run under the DDL lock timeout + idle-blocker watcher
        # (design §4.4): a previous run's writer idle in a transaction on chkpoint must not hang setup.
        self._exec(stmts, which="admin")
        tgt_role = _check(self.dsn.get("target_user", "target"))
        tgt_schema = _check(self.dsn.get("target_schema", tgt_role))
        self._ddl_reporting([
            f'CREATE TABLE IF NOT EXISTS "{tgt_schema}"."chkpoint" (id varchar(100) PRIMARY KEY, '
            f'sourceposition bytea, pendingddl numeric(1), ddl text)',
            f"DO $$ BEGIN IF (SELECT tableowner FROM pg_tables WHERE schemaname = '{tgt_schema}' "
            f"AND tablename = 'chkpoint') <> '{tgt_role}' THEN "
            f'ALTER TABLE "{tgt_schema}"."chkpoint" OWNER TO {tgt_role}; END IF; END $$'])

    def reset_schemas(self) -> list[dict]:
        # Per-test clean slate (serial runs, fixed schemas): drop + recreate the qasource
        # and qatarget schemas as the admin. CASCADE clears any tables a prior test left.
        # Returns the idle-in-transaction backends it had to terminate first (design §4.4).
        stmts = []
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            schema = _check(self.dsn.get(f"{which}_schema", role))
            stmts.append(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            stmts.append(f'CREATE SCHEMA "{schema}" AUTHORIZATION {role}')
        return self._ddl_reporting(stmts)

    def _ddl_reporting(self, stmts: list[str]) -> list[dict]:
        # Runs admin DDL under the idle-blocker watcher (design §4.4) and returns the backends it terminated for it.
        before = len(self.terminated)
        self._exec(stmts, which="admin", lock_timeout_ms=self.DDL_LOCK_TIMEOUT_MS)
        return self.terminated[before:]

    def reset_test_objects(self, tid: str) -> list[dict]:
        """Per-test clean slate SAFE under concurrent workers (spec §C.4): drop only THIS
        test's ${tid}-prefixed tables in the shared qasource/qatarget schemas — NOT the
        whole-schema DROP CASCADE (reset_schemas), which would clobber a sibling worker's
        tables mid-run. A same-test re-run reuses the deterministic tid, so its prior tables
        must go first (postgres ddl uses bare CREATE TABLE, no IF EXISTS). Mirrors Oracle's
        per-test table-prefix isolation.

        pg lowercases unquoted identifiers and the tid slug is already lowercase; underscores
        in the tid are escaped so they aren't LIKE wildcards. `tid` already includes its own
        trailing '_' separator (e.g. `mytest_`), so it IS the full table-name prefix -- match
        `<tid>%`, not `<tid>_%`. (Caveat, as with any prefix scheme: a tid that is a prefix of
        another test's tid could over-match — the current corpus has no such colliding names.)"""
        prefix = tid.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"{prefix}%"
        terminated: list[dict] = []
        for which in ("source", "target"):
            role = _check(self.dsn.get(f"{which}_user", which))
            schema = _check(self.dsn.get(f"{which}_schema", role))
            _, rows = self._query(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s ESCAPE '\\'",
                params=(schema, like), which="admin")
            drops = [f'DROP TABLE IF EXISTS "{schema}"."{_check(t)}" CASCADE' for (t,) in rows]
            if drops:
                # The idle-blocker watcher terminates only what blocks THESE drops -- this test's own
                # tables -- so a sibling worker's writer idle on its own tables is untouched.
                terminated += self._ddl_reporting(drops)
        return terminated

    def create_schema(self, schema: str) -> None:
        self._exec([f'CREATE SCHEMA IF NOT EXISTS "{_check(schema)}"'], which="admin")

    def drop_schema(self, schema: str) -> list[dict]:
        return self._ddl_reporting([f'DROP SCHEMA IF EXISTS "{_check(schema)}" CASCADE'])

    def drop_replication_slot(self, name: str, on_active=None) -> None:
        # The app's STOP can return before its replication connection closes. Retry only
        # object_in_use, bounded; never terminate a backend to force the drop.
        n = _check(name)
        deadline = time.monotonic() + 10.0
        while True:
            try:
                self._exec([
                    f"SELECT pg_drop_replication_slot('{n}') "
                    f"FROM pg_replication_slots WHERE slot_name = '{n}'"
                ], which="admin")
                return
            except Exception as exc:
                if getattr(exc, "pgcode", None) != "55006" or time.monotonic() >= deadline:
                    raise
                if on_active is not None:
                    stop, on_active = on_active, None
                    stop(deadline - time.monotonic())
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def sweep_stale_replication_slots(self, log) -> None:
        # This connection is the resolved lane's PostgreSQL, using its admin credentials.
        # Slots are cluster-wide, so also require this database. Recheck inactivity at DROP.
        guard = "active = false AND database = current_database() AND slot_name ~ %s"
        _, slots = self._query(f"SELECT slot_name FROM pg_replication_slots WHERE {guard}",
                              (_HARNESS_SLOT_PATTERN,), which="admin")
        for (name,) in slots:
            try:
                _, dropped = self._query(
                    f"SELECT slot_name, pg_drop_replication_slot(slot_name) "
                    f"FROM pg_replication_slots WHERE slot_name = %s AND {guard}",
                    (name, _HARNESS_SLOT_PATTERN), which="admin")
            except Exception as exc:
                # A slot can become active or disappear after the catalog read. PostgreSQL
                # refuses an active slot; leave it alone and continue with the other slots.
                if getattr(exc, "pgcode", None) not in ("55006", "42704"):
                    raise
                continue
            if dropped:
                log(f"postgres: dropped stale replication slot {name}")

    def run_sql(self, sql: str) -> None:
        # Run DDL/seed as this instance's data role, in that role's schema. The schema is
        # fixed per role (qasource/qatarget), so — unlike the old per-test-schema model —
        # there is no schema argument (mirrors OraAdmin.run_sql(sql)).
        self._exec([f'SET search_path TO "{self._schema()}"', sql])

    def _qualified(self, table: str) -> str:
        parts = table.split(".")
        if len(parts) != 2:
            raise ValueError(f"table must be schema.table: {table!r}")
        return ".".join(f'"{_check(p)}"' for p in parts)

    def _query(self, sql: str, params=None, which: str | None = None):
        conn = self._open(which)
        try:
            cur = conn.cursor()
            try:
                # Keep the no-param call shape (cur.execute(sql)) for existing callers —
                # only pass params when given, so parameterized reads (reset_test_objects)
                # bind safely without changing the count/select read paths.
                if params is None:
                    cur.execute(sql)
                else:
                    cur.execute(sql, params)
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
            out.append({c: _coerce(v) for c, v in zip(cols, r)})
        return out
