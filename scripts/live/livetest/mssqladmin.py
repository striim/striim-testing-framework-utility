from __future__ import annotations
import os
import re
import time
from livetest.sqlutil import coerce_cell

# SQL Server admin for pytest's own connection (setup + diff/data reads), backed by
# pymssql. Mirrors OraAdmin: split-and-run statements with autocommit, str-coerced reads,
# a qualified-table guard. Distinct from what the STRIIM app uses (the MSSQL_* tokens).

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A normalized sa password safe to inline into ALTER LOGIN (letters/digits/underscore).
_SIMPLE_PW = re.compile(r"^[A-Za-z0-9_]+$")
# Complex password the slt-mssql container BOOTS with (compose.yaml MSSQL_SA_PASSWORD) —
# SQL Server refuses to start with a weak sa password. ensure_setup relaxes sa to the
# normalized weak password afterwards. Override with SLT_MSSQL_BOOTSTRAP_PASSWORD.
_BOOTSTRAP_SA_PASSWORD = "QAtestuser1"
# SQL Server startup / first-connect transients (login before the server is fully up,
# or the app DB still coming online). NB: no bare "database" substring — it matched
# genuine PERMANENT errors ("Cannot open database ...") and retried them for 60s.
_TRANSIENT = ("20009", "18456", "Login failed", "not currently available",
              "Adaptive Server connection failed", "Connection refused")


def _split_statements(sql: str) -> list[str]:
    # Drop line comments and batch separators (GO is a client directive, not T-SQL),
    # split on ';'. Good enough for the setup/ddl/seed this framework runs.
    from livetest.sqlutil import split_sql_statements
    # GO is a client batch separator (not T-SQL); drop those lines, then string-aware split.
    no_go = "\n".join(ln for ln in sql.splitlines() if ln.strip().upper() != "GO")
    return split_sql_statements(no_go)


def _check_table(table: str) -> str:
    # Accept schema.table (dbo.SRC) or bare table (-> dbo). Validate each part so the
    # name can be interpolated into a read query safely.
    parts = table.split(".")
    if len(parts) == 1:
        parts = ["dbo", parts[0]]
    if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"table must be schema.table with safe identifiers: {table!r}")
    return f"[{parts[0]}].[{parts[1]}]"


class MssqlAdmin:
    """SQL Server admin. Consistent with the other DBs, a test's MSSQL carries the `sa`
    admin plus qasource/qatarget logins+users, each defaulting to a same-named schema in
    the app DB. One MssqlAdmin drives ONE data role (role="source"->qasource,
    "target"->qatarget) for DDL/seed/reads; setup (sa-password relax, DB create, CDC
    enable, account creation) uses the `sa` admin connection. dsn keys: user/password (sa,
    the admin), source_user/source_password/source_schema, target_user/target_password/
    target_schema — absent role keys fall back to the admin so a minimal dsn still works."""

    def __init__(self, dsn: dict, connect=None, role: str = "source"):
        self.dsn = dsn
        self.role = role
        if connect is not None:
            self._connect = connect
        else:
            import pymssql
            self._connect = pymssql.connect

    def _creds(self, which: str):
        # which: "admin" | "source" | "target". admin == the base sa login; source/target
        # fall back to the admin creds when their keys are absent.
        if which == "admin":
            return self.dsn["user"], self.dsn["password"]
        return (self.dsn.get(f"{which}_user", self.dsn["user"]),
                self.dsn.get(f"{which}_password", self.dsn["password"]))

    def _open(self, database: str | None = None, attempts: int = 30, delay: float = 2.0,
              password: str | None = None, which: str | None = None):
        user, pw = self._creds(which or self.role)
        last = None
        for _ in range(attempts):
            conn = None
            try:
                conn = self._connect(
                    server=self.dsn["host"], port=str(self.dsn["port"]),
                    user=user, password=password if password is not None else pw,
                    database=database if database is not None else self.dsn.get("database", "master"),
                    autocommit=True,
                )
                cur = conn.cursor()
                cur.execute("SELECT 1")           # probe: server accepting queries
                cur.fetchall()
                return conn
            except Exception as e:                # noqa: BLE001 — retried below
                last = e
                if conn is not None:              # don't leak the probe-failed connection
                    try:
                        conn.close()
                    except Exception:
                        pass
                if any(t.lower() in str(e).lower() for t in _TRANSIENT):
                    time.sleep(delay)
                    continue
                raise
        raise last

    def _ensure_sa_password(self) -> None:
        # Normalize the sa login to the configured (weak) password. SQL Server refuses to
        # BOOT with a weak sa password, so slt-mssql starts on a complex BOOTSTRAP password
        # and we relax sa to the target here (ALTER LOGIN ... CHECK_POLICY=OFF). Idempotent:
        # if sa is already the target we do nothing; a fresh container is migrated once.
        #
        # For a Docker-provisioned container, entrypoint.sh's own background init.sql is
        # the AUTHORITATIVE migration, gated behind the healthcheck (services/mssql/compose.yaml)
        # — by the time `docker compose up --wait` returns, sa is already the target
        # password, so the fast-path probe below succeeds immediately and the ALTER LOGIN
        # fallback is never reached. That fallback still matters for SLT_MSSQL_HOST (a live
        # external server this framework doesn't control the boot sequence of). Do not
        # "optimize" this by connecting eagerly before the container reports healthy — three
        # actors independently probing/ALTERing sa at boot is exactly the race that used to
        # cause intermittent "Login failed" errors here (see compose.yaml's healthcheck comment).
        target = self.dsn["password"]
        bootstrap = os.environ.get("SLT_MSSQL_BOOTSTRAP_PASSWORD") or _BOOTSTRAP_SA_PASSWORD
        if target == bootstrap:
            return                       # nothing to relax (target already meets complexity)
        # Already migrated? (short probe — the server is already up by resolve time, so a
        # login failure here means "wrong password", not "still starting".)
        try:
            self._open(database="master", password=target, attempts=2, delay=1.0, which="admin").close()
            return
        except Exception:                # noqa: BLE001 — fall through to the bootstrap migration
            pass
        login = self.dsn["user"]
        if not _IDENT.match(login) or not _SIMPLE_PW.match(target):
            raise ValueError(f"unsafe sa login/password for ALTER LOGIN: {login!r}")
        conn = self._open(database="master", password=bootstrap, which="admin")
        try:
            # ALTER LOGIN is DDL — the password cannot be bound as a parameter, so inline
            # the (validated, letters/digits/underscore) values.
            conn.cursor().execute(
                f"ALTER LOGIN [{login}] WITH PASSWORD = '{target}', "
                f"CHECK_POLICY = OFF, CHECK_EXPIRATION = OFF")
        finally:
            conn.close()

    def ensure_setup(self) -> None:
        # All setup runs as the sa admin: relax the sa password (see _ensure_sa_password),
        # create the app database, enable CDC, then create the qasource/qatarget data
        # accounts + schemas. Idempotent; runs once per resolve.
        self._ensure_sa_password()
        db = self.dsn.get("database", "qauser")
        if not _IDENT.match(db):
            raise ValueError(f"unsafe database name: {db!r}")
        conn = self._open(database="master", which="admin")
        try:
            conn.cursor().execute(f"IF DB_ID('{db}') IS NULL CREATE DATABASE [{db}]")
        finally:
            conn.close()
        conn = self._open(database=db, which="admin")
        try:
            cur = conn.cursor()
            cur.execute("SELECT is_cdc_enabled FROM sys.databases WHERE name=DB_NAME()")
            if not cur.fetchone()[0]:
                try:
                    cur.execute("EXEC sys.sp_cdc_enable_db")
                except Exception as plain:
                    # Cloud SQL for SQL Server refuses sys.sp_cdc_enable_db and wraps it in its
                    # own procedure; a managed server that has neither is still usable as a
                    # TARGET, which needs no CDC at all. Only a SOURCE case needs it, and one
                    # would fail on its own first read.
                    try:
                        cur.execute(f"EXEC msdb.dbo.gcloudsql_cdc_enable_db N'{db}'")
                    except Exception as managed:
                        print(f"[mssql] CDC not enabled on {db}: {plain!s:.120} / "
                              f"{managed!s:.120} -- fine for a target, not for a source")
        finally:
            conn.close()
        self._ensure_data_accounts(db)

    def _ensure_data_accounts(self, db: str) -> None:
        # Create the qasource/qatarget SQL logins (weak password needs CHECK_POLICY OFF, as
        # for sa), their users in `db` defaulting to a same-named schema, and those schemas.
        # Members of db_owner so each role can create/seed/read its own tables and the CDC
        # metadata (test DB — broad rights are fine). Idempotent. Runs as the sa admin.
        accounts = []
        for which in ("source", "target"):
            user, pw = self._creds(which)
            schema = self.dsn.get(f"{which}_schema", user)
            if not _IDENT.match(user) or not _IDENT.match(schema) or not _SIMPLE_PW.match(pw):
                raise ValueError(f"unsafe {which} login/schema/password: {user!r}/{schema!r}")
            accounts.append((user, pw, schema))
        # Server-level logins live in master.
        conn = self._open(database="master", which="admin")
        try:
            cur = conn.cursor()
            for user, pw, _ in accounts:
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name=N'{user}') "
                    f"CREATE LOGIN [{user}] WITH PASSWORD='{pw}', CHECK_POLICY=OFF, CHECK_EXPIRATION=OFF")
        finally:
            conn.close()
        # DB users + schemas + db_owner membership live in the app DB.
        conn = self._open(database=db, which="admin")
        try:
            cur = conn.cursor()
            for user, _, schema in accounts:
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name=N'{user}') "
                    f"CREATE USER [{user}] FOR LOGIN [{user}] WITH DEFAULT_SCHEMA=[{schema}]")
                # CREATE SCHEMA must be the first statement in its batch -> wrap in EXEC.
                cur.execute(
                    f"IF SCHEMA_ID(N'{schema}') IS NULL "
                    f"EXEC('CREATE SCHEMA [{schema}] AUTHORIZATION [{user}]')")
                cur.execute(f"ALTER ROLE db_owner ADD MEMBER [{user}]")
        finally:
            conn.close()

    def run_sql(self, sql: str) -> None:
        conn = self._open()
        try:
            cur = conn.cursor()
            for stmt in _split_statements(sql):
                cur.execute(stmt)
        finally:
            conn.close()

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this role's tables in its fixed schema
        (qasource/qatarget). prefix "" drops ALL tables in the schema -- serial runs,
        mirroring the Postgres whole-schema reset; a non-empty prefix (the per-test
        ${TID}) drops only this test's tables so a concurrent sibling's tables survive
        (SQL Server's default collation is case-insensitive, so match that way).
        Multi-pass: a table referenced by another's FK can only drop after its
        dependents, so retry until no progress. Best-effort teardown."""
        if self.role == "admin":
            return    # the admin route owns no test tables; never sweep as sa
        user, _ = self._creds(self.role)
        schema = self.dsn.get(f"{self.role}_schema", user)
        if not _IDENT.match(schema):
            raise ValueError(f"unsafe schema name: {schema!r}")
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name = %s", (schema,))
            tables = [r[0] for r in cur.fetchall()]
            if prefix:
                low = prefix.lower()
                tables = [t for t in tables if t.lower().startswith(low)]
            remaining = [t for t in tables if _IDENT.match(t)]
            # DISABLE CDC BEFORE DROPPING. `DROP TABLE` on a CDC-tracked table leaves its capture
            # instance registered until SQL Server's own cleanup job removes it. That window was
            # MEASURED at ~6 seconds on 2026-09-17 (table gone at 17:14:40 with
            # qasource_KPS_ORDERS still registered; registration gone by 17:14:46), and the NEXT
            # case's `sp_cdc_enable_table` landing inside it fails with
            #   22926: capture instance name 'qasource_KPS_ORDERS' already exists
            # It is a RACE against the cleanup job, which is why it looked non-reproducible:
            # tier3/tier4 cleared the window and tier5/tier7 did not. Same run, same tables --
            # only the timing differed.
            #
            # The cases' own DDL files already do disable-then-drop in exactly this order
            # (`IF EXISTS(... is_tracked_by_cdc = 1) EXEC sp_cdc_disable_table
            # @capture_instance = N'all'` then `DROP TABLE IF EXISTS`). Teardown did not, so the
            # tree was internally inconsistent about its own convention. Doing it here closes the
            # window for EVERY case rather than for the one pair that happened to collide.
            #
            # NOT THE FIX, recorded so it is not re-proposed: restoring a per-test ${TID} so the
            # two cases stop sharing `KPS_ORDERS`. Fixed table names under serial execution is the
            # DOCUMENTED design -- services/mssql/service.yaml declares
            # `isolation: none  # shared qauser DB; tests use fixed tables + run serially` -- so
            # isolating by name would fix the symptom by breaking the design it rests on.
            #
            # Best-effort, matching the drop loop's posture below: a database without CDC enabled,
            # or a table already untracked, must not fail teardown.
            for t in remaining:
                try:
                    cur.execute(
                        "SELECT 1 FROM sys.tables t JOIN sys.schemas s "
                        "ON t.schema_id = s.schema_id "
                        "WHERE s.name = %s AND t.name = %s AND t.is_tracked_by_cdc = 1",
                        (schema, t))
                    if cur.fetchone():
                        cur.execute(
                            "EXEC sys.sp_cdc_disable_table @source_schema = %s, "
                            "@source_name = %s, @capture_instance = N'all'", (schema, t))
                except Exception:      # noqa: BLE001 -- best-effort teardown, as below
                    pass
            while remaining:
                failed = []
                for t in remaining:
                    try:
                        cur.execute(f"DROP TABLE [{schema}].[{t}]")
                    except Exception:      # noqa: BLE001 -- FK ordering; retried below
                        failed.append(t)
                if len(failed) == len(remaining):
                    break   # no progress -- leave the rest (best-effort)
                remaining = failed
        finally:
            conn.close()

    def count_rows(self, table: str) -> int:
        # Polled while a writer is loading the table. A shared-lock COUNT(*) contends with that
        # writer's transaction and SQL Server has picked the poller as the deadlock victim (§70.8),
        # so this reads uncommitted -- the count only has to say whether the number is still
        # moving, and once the writer is done and committed it is exact.
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET DEADLOCK_PRIORITY LOW; "
                        f"SELECT COUNT(*) FROM {_check_table(table)}")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def count_rows_committed(self, table: str) -> int:
        # The dirty count above says the writer has EXECUTED its rows; this says it has COMMITTED
        # them. An exact `rows:` assertion confirms with this once the dirty count is satisfied, so
        # a transaction that later rolls back cannot have passed it.
        # Lock timeout rather than NOLOCK: if the writer still holds the table, wait briefly and
        # let the poll loop come back, never deadlock against it.
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute("SET LOCK_TIMEOUT 5000; SET DEADLOCK_PRIORITY LOW; "
                        f"SELECT COUNT(*) FROM {_check_table(table)}")
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def select_rows(self, table: str) -> list[dict]:
        conn = self._open()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {_check_table(table)}")
            cols = [d[0] for d in cur.description]
            return [{c: coerce_cell(v) for c, v in zip(cols, row)}
                    for row in cur.fetchall()]
        finally:
            conn.close()
