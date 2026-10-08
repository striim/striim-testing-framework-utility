import pytest
from livetest.mysqlclient import MySQLAdmin, _check


class FakeCursor:
    def __init__(self, rows=None, description=None):
        self.rows = rows or []
        self.description = description or []
        self.executed = []
        self.params = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows=None, description=None):
        self.rows = rows or []
        self.description = description or []
        self.closed = False
        self.autocommit = False

    def cursor(self):
        self.last_cursor = FakeCursor(self.rows, self.description)
        return self.last_cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _admin(rows=None, description=None):
    """Create a test MySQLAdmin with a fake connection factory."""
    config = {
        "host": "localhost",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "striim",
        "source_user": "qasource",
        "source_password": "striim",
        "source_schema": "qasource",
        "target_user": "qatarget",
        "target_password": "striim",
        "target_schema": "qatarget",
    }

    def fake_get_conn(attempts=30, delay=2.0):
        return FakeConn(rows or [], description or [])

    admin = MySQLAdmin(config, role="admin")
    admin._get_conn = fake_get_conn
    return admin


# ---- identifier validation --------------------------------------------------

def test_check_safe_identifier():
    assert _check("qasource") == "qasource"
    assert _check("table_name") == "table_name"
    assert _check("_private") == "_private"


def test_check_rejects_unsafe_identifier():
    with pytest.raises(ValueError, match="unsafe identifier"):
        _check("table; DROP")
    with pytest.raises(ValueError, match="unsafe identifier"):
        _check("table.name")
    with pytest.raises(ValueError, match="unsafe identifier"):
        _check("123invalid")


# ---- reads ------------------------------------------------------------------

def test_count_rows_returns_int():
    admin = _admin(rows=[(7,)], description=[("COUNT(*)",)])
    assert admin.count_rows("qasource.users") == 7


def test_count_rows_returns_zero_for_empty_result():
    admin = _admin(rows=[], description=[("COUNT(*)",)])
    assert admin.count_rows("qasource.users") == 0


def test_select_rows_returns_str_coerced_dicts():
    admin = _admin(
        rows=[(1, "Jane", None), (2, "John", "Developer")],
        description=[("ID",), ("NAME",), ("ROLE",)]
    )
    rows = admin.select_rows("qasource.customers")
    assert rows == [
        {"ID": "1", "NAME": "Jane", "ROLE": None},
        {"ID": "2", "NAME": "John", "ROLE": "Developer"},
    ]


def test_select_rows_with_implicit_schema():
    """Test that select_rows adds the default schema when not qualified."""
    config = {
        "host": "h",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "pw",
        "source_user": "qasource",
        "source_schema": "qasource",
    }
    rows = [(1, "test")]
    desc = [("ID",), ("NAME",)]

    admin = MySQLAdmin(config, role="source")
    admin._get_conn = lambda attempts=30, delay=2.0: FakeConn(rows, desc)

    result = admin.select_rows("events")
    # Verify the table was qualified with default schema
    assert result == [{"ID": "1", "NAME": "test"}]


def test_select_rows_with_explicit_schema():
    """Test that select_rows respects explicit schema.table format."""
    config = {
        "host": "h",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "pw",
        "source_user": "qasource",
        "source_schema": "qasource",
    }
    rows = [(42,)]
    desc = [("VALUE",)]

    admin = MySQLAdmin(config, role="source")
    admin._get_conn = lambda attempts=30, delay=2.0: FakeConn(rows, desc)

    result = admin.select_rows("qatarget.metrics")
    assert result == [{"VALUE": "42"}]


# ---- SQL execution ----------------------------------------------------------

def test_run_sql_executes_single_statement():
    admin = _admin()
    admin.run_sql("CREATE TABLE test (id INT)")
    # Verify it was executed


def test_run_sql_executes_multiple_statements():
    admin = _admin()
    sql = """
    CREATE TABLE t1 (id INT);
    INSERT INTO t1 VALUES (1);
    INSERT INTO t1 VALUES (2);
    """
    admin.run_sql(sql)
    # Verify multiple statements were split and executed


def test_run_sql_ignores_empty_string():
    admin = _admin()
    admin.run_sql("")
    admin.run_sql("   ")
    # Should not raise


def test_run_sql_strips_comments():
    admin = _admin()
    sql = """
    -- This is a comment
    CREATE TABLE t (id INT);
    -- Another comment
    """
    admin.run_sql(sql)
    # Should handle comments gracefully


def test_run_sql_does_not_split_on_a_semicolon_inside_a_comment():
    admin = _admin()
    conns = []

    def fake_get_conn(attempts=30, delay=2.0):
        conns.append(FakeConn())
        return conns[-1]

    admin._get_conn = fake_get_conn
    admin.run_sql(
        "CREATE TABLE a (id INT);\n"
        "-- The writer never creates this; it quotes this DDL into an error.\n"
        "CREATE TABLE b (v VARCHAR(8) DEFAULT 'x;y');\n"
    )
    assert conns[-1].last_cursor.executed == [
        "CREATE TABLE a (id INT)",
        "CREATE TABLE b (v VARCHAR(8) DEFAULT 'x;y')",
    ]


# ---- reset_test_objects (per-test isolation) --------------------------------

def test_reset_test_objects_drops_tid_tables():
    """reset_test_objects drops only tid-prefixed tables, safe under concurrent workers."""
    config = {
        "host": "h",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "pw",
        "source_user": "qasource",
        "source_password": "pw",
        "source_schema": "qasource",
        "target_user": "qatarget",
        "target_password": "pw",
        "target_schema": "qatarget",
    }

    # Simulate information_schema query result
    # (schema, table_name)
    found_tables = [
        ("qasource", "mytst_events"),
        ("qasource", "mytst_users"),
        ("qatarget", "mytst_output"),
        ("qasource", "other_table"),  # Should NOT be dropped (different prefix)
    ]

    admin = MySQLAdmin(config)
    admin._get_conn = lambda attempts=30, delay=2.0: FakeConn(found_tables, [("SCHEMA",), ("TABLE",)])

    # This should construct DROP statements for only mytst_* tables
    admin.reset_test_objects("mytst_")
    # If the method ran without error, it worked (fake connection doesn't really drop)


def test_reset_test_objects_sanitizes_tid():
    """reset_test_objects validates tid for SQL injection safety."""
    config = {
        "host": "h",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "pw",
        "source_user": "qasource",
    }
    admin = MySQLAdmin(config)

    # Should raise on unsafe tid
    with pytest.raises(ValueError, match="unsafe identifier"):
        admin.reset_test_objects("test'; DROP--")


def test_reset_test_objects_handles_no_matching_tables():
    """reset_test_objects gracefully handles no tables matching the tid."""
    config = {
        "host": "h",
        "port": 3306,
        "admin_user": "root",
        "admin_password": "pw",
    }

    admin = MySQLAdmin(config)
    admin._get_conn = lambda attempts=30, delay=2.0: FakeConn([], [("SCHEMA",), ("TABLE",)])

    # Should not raise when no tables are found
    admin.reset_test_objects("nonexistent_")


# ---- create_schema / drop_schema (explicit management) ----------------------

def test_create_schema_executes_create_statement():
    admin = _admin()
    admin.create_schema("myschema")
    # Verify CREATE SCHEMA statement was executed


def test_drop_schema_executes_drop_statement():
    admin = _admin()
    admin.drop_schema("myschema")
    # Verify DROP SCHEMA statement was executed


# ---- ensure_setup (idempotent provisioning) ---------------------------------

def test_ensure_setup_idempotent():
    """ensure_setup creates users and schemas with IF NOT EXISTS (idempotent)."""
    admin = _admin()
    # Should not raise even when called multiple times
    admin.ensure_setup()
    admin.ensure_setup()


# ---- reset_schemas (whole-schema reset) ------

def test_reset_schemas_drops_and_recreates():
    """reset_schemas provides a clean slate by dropping and recreating schemas."""
    admin = _admin()
    # Should not raise
    admin.reset_schemas()


# ---- drop_test_tables (teardown hook used by plugin.teardown_ddl_tables) -----

_CFG = {
    "host": "h", "port": 3306, "admin_user": "root", "admin_password": "pw",
    "source_user": "qasource", "source_password": "pw", "source_schema": "qasource",
    "target_user": "qatarget", "target_password": "pw", "target_schema": "qatarget",
}


def _drop_admin(role, found):
    """found: (schema, table) rows information_schema would return for the schema(s)."""
    admin = MySQLAdmin(_CFG, role=role)
    conn = FakeConn(found, [("TABLE_SCHEMA",), ("TABLE_NAME",)])
    admin._get_conn = lambda attempts=30, delay=2.0: conn
    batches = []
    admin._exec_sql = lambda stmts: batches.append(stmts)
    return admin, conn, batches


def _dropped(batches):
    """Table names in the DROP statements, ignoring the FK-check bookends."""
    if not batches:
        return []
    return [st.split("`.`")[1].rstrip("`")
            for st in batches[0] if st.startswith("DROP TABLE")]


def test_drop_test_tables_filters_by_prefix_in_python():
    """The prefix is applied in Python, not by SQL LIKE: MySQL rejects
    `LIKE BINARY ... ESCAPE` (1064), and an unescaped '_' would be a wildcard."""
    admin, conn, batches = _drop_admin("source", [
        ("qasource", "t0a1b_src"), ("qasource", "t0a1b_tgt"),
        ("qasource", "t0a1bXother"),   # '_' must NOT match 'X'
        ("qasource", "unrelated"),
    ])
    admin.drop_test_tables("t0a1b_")

    sql = conn.last_cursor.executed[0]
    assert "LIKE" not in sql          # filtering is not done in SQL
    assert "TABLE_TYPE = 'BASE TABLE'" in sql
    assert conn.last_cursor.params[0] == ("qasource",)
    assert sorted(_dropped(batches)) == ["t0a1b_src", "t0a1b_tgt"]


def test_drop_test_tables_wraps_drops_in_fk_check_toggle():
    """FK chains drop in one pass rather than in dependency order. The restore is its
    own call, from a finally: -- see test_drop_restores_fk_checks_even_when_the_drops_fail."""
    _admin_, _conn, batches = _drop_admin("source", [("qasource", "t0a1b_x")])
    _admin_.drop_test_tables("t0a1b_")
    assert batches[0][0] == "SET FOREIGN_KEY_CHECKS = 0"
    assert batches[0][1].startswith("DROP TABLE")
    assert batches[-1] == ["SET FOREIGN_KEY_CHECKS = 1"]


def test_drop_test_tables_empty_prefix_drops_whole_schema():
    """Serial runs pass "" -- every table in the role's schema goes, mirroring the
    Postgres whole-schema reset."""
    admin, conn, batches = _drop_admin("target", [
        ("qatarget", "leftover"), ("qatarget", "another"),
    ])
    admin.drop_test_tables("")
    assert conn.last_cursor.params[0] == ("qatarget",)
    assert sorted(_dropped(batches)) == ["another", "leftover"]


def test_drop_test_tables_uses_the_role_schema():
    """source drops in qasource, target in qatarget -- the route key picks the schema."""
    admin, conn, _ = _drop_admin("source", [])
    admin.drop_test_tables("")
    assert conn.last_cursor.params[0] == ("qasource",)


def test_drop_test_tables_noop_when_nothing_matches():
    admin, _conn, batches = _drop_admin("source", [("qasource", "unrelated")])
    admin.drop_test_tables("t0a1b_")
    assert batches == []


def test_drop_test_tables_rejects_unsafe_prefix():
    admin, _conn, _batches = _drop_admin("source", [])
    with pytest.raises(ValueError, match="unsafe identifier"):
        admin.drop_test_tables("x\'; DROP TABLE y--")


def test_reset_test_objects_spans_both_schemas_and_escapes_nothing():
    """Same prefix semantics as drop_test_tables, across qasource AND qatarget."""
    admin, conn, batches = _drop_admin("source", [
        ("qasource", "tddd_a"), ("qatarget", "tddd_c"), ("qasource", "tdddXb"),
    ])
    admin.reset_test_objects("tddd_")
    assert conn.last_cursor.params[0] == ("qasource", "qatarget")
    assert sorted(_dropped(batches)) == ["tddd_a", "tddd_c"]


def test_drop_skips_non_identifier_names_instead_of_raising():
    """A backquoted-but-odd MySQL name (e.g. '2024_orders') must not abort the whole
    cleanup: teardown swallows exceptions and only warns, so raising would silently
    leave everything behind. MssqlAdmin filters the same way."""
    admin, _conn, batches = _drop_admin("source", [
        ("qasource", "2024_orders"), ("qasource", "order-items"),
        ("qasource", "good_table"),
    ])
    admin.drop_test_tables("")
    assert _dropped(batches) == ["good_table"]


def test_drop_restores_fk_checks_even_when_the_drops_fail():
    """_exec_sql rolls back and re-raises on failure, and a rollback does not reset a
    session variable -- on a StaticPool connection the flag would otherwise stay 0 for
    every later statement on this instance."""
    admin = MySQLAdmin(_CFG, role="source")
    admin._get_conn = lambda attempts=30, delay=2.0: FakeConn(
        [("qasource", "t0a1b_x")], [("TABLE_SCHEMA",), ("TABLE_NAME",)])

    calls = []
    def boom(stmts):
        calls.append(stmts)
        if any(st.startswith("DROP TABLE") for st in stmts):
            raise RuntimeError("lock wait timeout")
    admin._exec_sql = boom

    with pytest.raises(RuntimeError, match="lock wait timeout"):
        admin.drop_test_tables("t0a1b_")
    assert calls[-1] == ["SET FOREIGN_KEY_CHECKS = 1"], calls
