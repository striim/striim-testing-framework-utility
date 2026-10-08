"""MySQL admin client for the live test framework.

Manages user provisioning, the fixed qasource/qatarget schemas, DDL/DML execution and
per-test cleanup. Schemas are NOT per-test: both are shared by every worker, and tests
are isolated by a ${TID} table-name PREFIX (`t<9 hex>_`, empty in serial runs).

- serial:   reset_schemas() drops and recreates both schemas
- parallel: reset_test_objects(tid) drops only this test's prefixed tables
- teardown: drop_test_tables(prefix), "" meaning the whole schema

Pattern matches PgAdmin (SQLAlchemy engine) and MssqlAdmin (prefix filtering in Python).
Uses PyMySQL with a SQLAlchemy StaticPool for a single serialized connection.
"""

from __future__ import annotations

import re
import time
from typing import Optional

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _check(name: str) -> str:
    """Validate schema/identifier name is safe for SQL interpolation."""
    if not _IDENT.match(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


class MySQLAdmin:
    """MySQL admin client for pytest lifecycle (setup, DDL/seed, reads).

    Uses fixed qasource/qatarget schemas (like Postgres/Oracle), not per-test isolation.
    Admin user (root) creates and tears down schemas; qasource/qatarget users own the
    tables and connect via appropriate role (source vs target).

    Uses SQLAlchemy engine with StaticPool (one connection per admin instance,
    serialized) to match PgAdmin pattern.
    """

    def __init__(self, config: dict, role: Optional[str] = None):
        """Initialize MySQL admin client.

        Args:
            config: {host, port, admin_user, admin_password, source_user, source_password,
                    source_schema, target_user, target_password, target_schema}
                    (from service.yaml provides dict)
            role: "source" or "target" (selects which user/schema to use for connections)
        """
        self.config = config
        self.role = role or "admin"
        self._engine = None
        self._init_engine()

    def _init_engine(self):
        """Initialize SQLAlchemy engine with StaticPool (single connection)."""
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        # Admin connection uses root credentials for setup/teardown
        user = self.config.get("admin_user", "root")
        password = self.config.get("admin_password", "striim")
        host = self.config.get("host", "localhost")
        port = self.config.get("port", 3306)

        url = f"mysql+pymysql://{user}:{password}@{host}:{port}/"

        self._engine = create_engine(
            url,
            poolclass=StaticPool,  # Single connection per instance (serialized)
            echo=False,
            connect_args={"charset": "utf8mb4", "autocommit": False},
            pool_pre_ping=True,  # Test connection before use
        )

    def _get_conn(self, attempts: int = 30, delay: float = 2.0):
        """Get a connection with retry for cold-start transients.

        MySQL container may report healthy before fully accepting connections.
        Retry for a bounded window before giving up.
        """
        from sqlalchemy.exc import OperationalError

        last_error = None
        for attempt in range(attempts):
            try:
                conn = self._engine.raw_connection()
                # Probe: verify the connection is truly usable
                cursor = conn.cursor()
                try:
                    cursor.execute("SELECT 1")
                finally:
                    cursor.close()
                return conn
            except (OperationalError, Exception) as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(delay)
        raise last_error or RuntimeError("Failed to connect to MySQL")

    def _exec_sql(self, statements: list[str]) -> None:
        """Execute a list of SQL statements with autocommit.

        Args:
            statements: List of SQL strings to execute in order
        """
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            try:
                for stmt in statements:
                    if stmt.strip():
                        cursor.execute(stmt)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()
        finally:
            conn.close()

    def ensure_setup(self) -> None:
        """Create qasource/qatarget users and schemas (idempotent).

        Creates:
        - qasource user (source role, for reading/seeding)
        - qatarget user (target role, for writing)
        - qasource schema (source tables)
        - qatarget schema (target tables)

        Idempotent: safe to call multiple times. Runs once per session.
        """
        statements = [
            # Create source user + schema (mysql_native_password required for binlog CDC library)
            "CREATE USER IF NOT EXISTS 'qasource'@'%' IDENTIFIED WITH mysql_native_password BY 'striim'",
            "ALTER USER 'qasource'@'%' IDENTIFIED WITH mysql_native_password BY 'striim'",
            "CREATE SCHEMA IF NOT EXISTS qasource",
            "GRANT ALL PRIVILEGES ON qasource.* TO 'qasource'@'%'",
            "GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'qasource'@'%'",

            # Create target user + schema
            "CREATE USER IF NOT EXISTS 'qatarget'@'%' IDENTIFIED WITH mysql_native_password BY 'striim'",
            "ALTER USER 'qatarget'@'%' IDENTIFIED WITH mysql_native_password BY 'striim'",
            "CREATE SCHEMA IF NOT EXISTS qatarget",
            "GRANT ALL PRIVILEGES ON qatarget.* TO 'qatarget'@'%'",

            # Flush privileges to ensure permissions take effect
            "FLUSH PRIVILEGES",
        ]
        self._exec_sql(statements)

    def reset_schemas(self) -> None:
        """Drop and recreate fixed qasource/qatarget schemas for clean slate.

        Called before each test (serial mode). Drops all tables in both schemas
        and recreates them to start fresh. Grants permissions to qasource/qatarget users.
        """
        statements = [
            "DROP SCHEMA IF EXISTS qasource",
            "DROP SCHEMA IF EXISTS qatarget",
            "CREATE SCHEMA qasource",
            "CREATE SCHEMA qatarget",
            "GRANT ALL PRIVILEGES ON qasource.* TO 'qasource'@'%'",
            "GRANT ALL PRIVILEGES ON qatarget.* TO 'qatarget'@'%'",
            "FLUSH PRIVILEGES",
        ]
        self._exec_sql(statements)

    def _list_tables(self, schemas: list[str], prefix: str = "") -> list[tuple[str, str]]:
        """(schema, table) for the BASE TABLEs in `schemas`, optionally filtered to those
        whose name starts with `prefix`.

        The prefix is matched in PYTHON, not with SQL LIKE. Two reasons: the tid ends in
        '_', which LIKE reads as a single-character wildcard unless escaped, and MySQL
        rejects `LIKE BINARY ... ESCAPE` outright (1064) -- so the escape clause that
        would fix it cannot be combined with the BINARY that makes the match
        case-sensitive. str.startswith is case-sensitive and needs no escaping, and
        matches MssqlAdmin's approach.
        """
        placeholders = ", ".join(["%s"] * len(schemas))
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES "
                f"WHERE TABLE_SCHEMA IN ({placeholders}) AND TABLE_TYPE = 'BASE TABLE'",
                tuple(schemas))
            rows = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        return [(sc, t) for sc, t in rows if not prefix or t.startswith(prefix)]

    def _drop(self, tables: list[tuple[str, str]]) -> None:
        """DROP each (schema, table), with FK checks off so a parent referenced by a
        child's FK does not have to be ordered after it.

        Names that are not plain identifiers are SKIPPED, not raised on -- MySQL allows
        them when backquoted, and this runs at teardown where teardown_ddl_tables
        swallows exceptions and only warns, so one odd name must not cost the whole
        cleanup. Mirrors MssqlAdmin.drop_test_tables, which filters the same way.

        FOREIGN_KEY_CHECKS is session-scoped and this pool is a single StaticPool
        connection, so it is restored in a finally: _exec_sql rolls back and re-raises
        on failure, and a rollback does not reset a session variable -- without this the
        flag would stay 0 for every later statement on this instance.
        """
        drops = [f"DROP TABLE IF EXISTS `{sc}`.`{t}`" for sc, t in tables
                 if _IDENT.match(sc) and _IDENT.match(t)]
        if not drops:
            return
        try:
            self._exec_sql(["SET FOREIGN_KEY_CHECKS = 0"] + drops)
        finally:
            self._exec_sql(["SET FOREIGN_KEY_CHECKS = 1"])

    def reset_test_objects(self, tid: str) -> None:
        """Per-test clean slate SAFE under concurrent workers (spec §C.4): drop only THIS
        test's ${tid}-prefixed tables in the shared qasource/qatarget schemas — NOT the
        whole-schema DROP (reset_schemas), which would clobber a sibling worker's tables
        mid-run. A same-test re-run reuses the deterministic tid, so its prior tables
        must go first.

        Args:
            tid: Test ID prefix including trailing '_' (e.g. 'mytest_')
        """
        prefix = _check(tid)
        self._drop(self._list_tables(["qasource", "qatarget"], prefix))

    def drop_test_tables(self, prefix: str = "") -> None:
        """Teardown cleanup: drop this role's tables in its fixed schema (qasource or
        qatarget). prefix "" drops EVERY table in the schema -- serial runs, mirroring
        the Postgres whole-schema reset; a non-empty prefix (this test's ${TID}) drops
        only this test's tables so a concurrent sibling's survive. Mirrors
        MssqlAdmin.drop_test_tables; plugin.teardown_ddl_tables finds it by name."""
        schema = _check(self._schema())
        self._drop(self._list_tables([schema], _check(prefix) if prefix else ""))

    def run_sql(self, sql: str) -> None:
        """Execute raw SQL for DDL/DML (test framework integration).

        Used by the framework to run test DDL/seed files. Supports multiple
        statements separated by ';' (a ';' inside a comment or quoted text does not
        separate; comments are dropped). Commits automatically.

        Args:
            sql: SQL string (may contain multiple statements)
        """
        from livetest.sqlutil import split_mysql_statements
        if not sql or not sql.strip():
            return

        self._exec_sql(split_mysql_statements(sql))

    def create_schema(self, name: str) -> None:
        """Create a named schema (for explicit schema management).

        Args:
            name: Schema name (validated for safety)
        """
        schema_name = _check(name)
        statements = [
            f"CREATE SCHEMA IF NOT EXISTS {schema_name}",
            f"GRANT ALL PRIVILEGES ON {schema_name}.* TO 'striim'@'%'",
        ]
        self._exec_sql(statements)

    def drop_schema(self, name: str) -> None:
        """Drop a named schema (for explicit cleanup).

        Args:
            name: Schema name (validated for safety)
        """
        schema_name = _check(name)
        statements = [f"DROP SCHEMA IF EXISTS {schema_name}"]
        self._exec_sql(statements)

    def _schema(self) -> str:
        return self.config.get(f"{self.role}_schema") or (
            "qasource" if self.role == "source" else "qatarget"
        )

    def _qualified(self, table: str) -> str:
        parts = table.split(".")
        if len(parts) == 1:
            return f"`{self._schema()}`.`{_check(parts[0])}`"
        elif len(parts) == 2:
            return f"`{_check(parts[0])}`.`{_check(parts[1])}`"
        raise ValueError(f"invalid table name: {table!r}")

    def count_rows(self, table: str) -> int:
        """Count rows in the specified table."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {self._qualified(table)}")
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def select_rows(self, table: str) -> list[dict]:
        """Select all rows from table as list of dicts with coerced cells."""
        from livetest.sqlutil import coerce_cell
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM {self._qualified(table)}")
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            return [{c: coerce_cell(v) for c, v in zip(cols, row)} for row in rows]
        finally:
            conn.close()

    def close(self) -> None:
        """Close the admin connection pool."""
        if self._engine:
            self._engine.dispose()
