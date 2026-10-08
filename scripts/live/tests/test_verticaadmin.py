"""VerticaAdmin, hermetically: pytest's own connection to a Vertica target (setup, DDL/seed,
data and diff reads)."""
import re
import types

import pytest

from livetest.verticaadmin import VerticaAdmin


def _dsn():
    return {"host": "h", "port": 5433, "dbname": "sltdb",
            "admin_user": "dbadmin", "admin_password": "striim",
            "source_user": "qasource", "source_password": "striim", "source_schema": "qasource",
            "target_user": "qatarget", "target_password": "striim", "target_schema": "qatarget"}


class _FakeDb:
    """A vertica_python-like connect() recording (user, sql, params); `results` maps a SQL
    prefix to the rows a query returns, `reject` is a set of users whose login fails."""

    def __init__(self, results=None, reject=()):
        self.executed = []
        self.logins = []
        self.databases = []
        self.results = results or {}
        self.reject = set(reject)

    def connect(self, host, port, database, user, password):
        self.logins.append(user)
        self.databases.append(database)
        if user in self.reject:
            raise Exception("Severity: FATAL, Message: Authentication failed for username "
                            f'"{user}", Sqlstate: 28000')
        rows = []

        def execute(sql, params=None):
            self.executed.append((user, sql, params))
            rows[:] = next((v(params) if callable(v) else v
                            for k, v in self.results.items() if sql.startswith(k)), [(1,)])

        cur = types.SimpleNamespace(execute=execute, fetchall=lambda: list(rows),
                                    description=[("A",), ("B",)])
        return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)


def _stmts(db, user=None):
    return [sql for u, sql, _ in db.executed if sql != "SELECT 1" and (user is None or u == user)]

# ---- setup ------------------------------------------------------------------

def test_ensure_setup_needs_no_admin_when_roles_are_ready():
    # a live instance may not hand out admin credentials; existing users need nothing
    db = _FakeDb(reject={"dbadmin"})
    VerticaAdmin(_dsn(), connect=db.connect).ensure_setup()
    assert "dbadmin" not in db.logins and not any(s.startswith(("CREATE", "ALTER")) for s in _stmts(db))


def test_ensure_setup_creates_a_missing_user_and_its_schema():
    db = _FakeDb(results={"SELECT 1 FROM v_catalog.users": []}, reject={"qatarget"})
    VerticaAdmin(_dsn(), connect=db.connect).ensure_setup()
    assert _stmts(db, "dbadmin") == [
        "SELECT 1 FROM v_catalog.users WHERE LOWER(user_name) = LOWER(:u)",
        "CREATE USER qatarget IDENTIFIED BY 'striim'",
        "CREATE SCHEMA IF NOT EXISTS qatarget AUTHORIZATION qatarget",
        "ALTER USER qatarget SEARCH_PATH qatarget, public"]


def test_ensure_setup_recreates_a_missing_schema_for_an_existing_user():
    # the user logs in but its schema is gone: no CREATE USER, only the schema and search path
    db = _FakeDb(results={"SELECT 1 FROM v_catalog.schemata": lambda p: [] if p == {"s": "qasource"} else [(1,)],
                          "SELECT 1 FROM v_catalog.users": [(1,)]})
    VerticaAdmin(_dsn(), connect=db.connect).ensure_setup()
    admin = _stmts(db, "dbadmin")
    assert not any(s.startswith("CREATE USER") for s in admin)
    assert "CREATE SCHEMA IF NOT EXISTS qasource AUTHORIZATION qasource" in admin
    assert not any("qatarget" in s for s in admin)


def test_ensure_setup_rejects_unsafe_password_when_it_must_create():
    dsn = {**_dsn(), "target_password": "x'; DROP USER dbadmin; --"}
    db = _FakeDb(results={"SELECT 1 FROM v_catalog.users": []}, reject={"qatarget"})
    with pytest.raises(ValueError, match="target"):
        VerticaAdmin(dsn, connect=db.connect).ensure_setup()
    assert not any(s.startswith("CREATE USER qatarget") for s in _stmts(db))


def test_ensure_setup_accepts_any_password_for_existing_users():
    # a live instance's existing users may have any password; nothing is inlined then
    dsn = {**_dsn(), "source_password": "P@ss-1", "target_password": "x y"}
    db = _FakeDb(results={"SELECT 1 FROM v_catalog.users": [(1,)]}, reject={"qasource"})
    VerticaAdmin(dsn, connect=db.connect).ensure_setup()
    assert not any(s.startswith("CREATE USER") for s in _stmts(db))

# ---- DDL/seed and teardown --------------------------------------------------

def test_run_sql_splits_statements_as_role_user_in_the_dsn_database():
    db = _FakeDb()
    VerticaAdmin(_dsn(), connect=db.connect, role="target").run_sql(
        "-- comment\nCREATE TABLE qatarget.T1 (a INT);\nINSERT INTO qatarget.T1 VALUES ('a;b');\n")
    assert _stmts(db) == ["SET SEARCH_PATH TO qatarget, public", "CREATE TABLE qatarget.T1 (a INT)", "INSERT INTO qatarget.T1 VALUES ('a;b')"]
    assert set(db.logins) == {"qatarget"} and set(db.databases) == {"sltdb"}


def test_drop_test_tables_prefix_is_case_insensitive_and_cascades():
    listing = [("t1abc_ORDERS",), ("T1ABC_ITEMS",), ("OTHER",)]
    db = _FakeDb(results={"SELECT table_name FROM v_catalog.tables": listing})
    VerticaAdmin(_dsn(), connect=db.connect).drop_test_tables("T1ABC_")
    assert [s for s in _stmts(db) if s.startswith("DROP")] == [
        "DROP TABLE IF EXISTS qasource.t1abc_ORDERS CASCADE",
        "DROP TABLE IF EXISTS qasource.T1ABC_ITEMS CASCADE"]
    listed = [p for _, s, p in db.executed if s.startswith("SELECT table_name")]
    assert listed == [{"s": "qasource"}]


def test_drop_test_tables_serial_drops_everything_and_skips_odd_names():
    db = _FakeDb(results={"SELECT table_name FROM v_catalog.tables": [("A1",), ("B2",), ("odd name",)]})
    VerticaAdmin(_dsn(), connect=db.connect, role="target").drop_test_tables("")
    assert [s for s in _stmts(db) if s.startswith("DROP")] == [
        "DROP TABLE IF EXISTS qatarget.A1 CASCADE", "DROP TABLE IF EXISTS qatarget.B2 CASCADE"]


def test_unsafe_prefix_is_refused():
    with pytest.raises(ValueError):
        VerticaAdmin(_dsn(), connect=_FakeDb().connect).drop_test_tables("t1'; DROP")


@pytest.mark.parametrize("prefix", ["", "T1ABC_"])
def test_admin_role_drop_is_a_no_op(prefix):
    # the serial sweep would otherwise list and drop every table in the admin's schema
    db = _FakeDb(results={"SELECT table_name FROM v_catalog.tables": [("A1",)]})
    VerticaAdmin(_dsn(), connect=db.connect, role="admin").drop_test_tables(prefix)
    assert db.logins == [] and db.executed == []


def test_admin_role_takes_its_credentials_from_the_dsn():
    db = _FakeDb()
    VerticaAdmin({**_dsn(), "admin_user": "vadmin", "admin_password": "pw"},
                 connect=db.connect, role="admin").run_sql("SELECT 2;")
    assert set(db.logins) == {"vadmin"}

# ---- reads ------------------------------------------------------------------

def test_qualified_defaults_to_role_schema_and_rejects_unsafe():
    adm = VerticaAdmin(_dsn(), connect=_FakeDb().connect, role="target")
    assert adm._qualified("T1") == "qatarget.T1"
    assert adm._qualified("qasource.T1") == "qasource.T1"
    with pytest.raises(ValueError):
        adm._qualified("qasource.T1; DROP TABLE x")


def test_count_and_select_rows():
    db = _FakeDb(results={"SELECT COUNT(*) FROM qasource.T1": [(3,)],
                          "SELECT * FROM qasource.T1": [(1, None), (2, "x")]})
    adm = VerticaAdmin(_dsn(), connect=db.connect)
    assert adm.count_rows("T1") == 3
    assert adm.select_rows("qasource.T1") == [{"A": "1", "B": None}, {"A": "2", "B": "x"}]


def test_no_committed_count_is_offered():
    # READ COMMITTED by default: assertions/data.py needs no separate committed count
    assert not hasattr(VerticaAdmin(_dsn(), connect=_FakeDb().connect), "count_rows_committed")

# ---- availability -----------------------------------------------------------

def test_open_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr("livetest.verticaadmin.time.sleep", lambda *_: None)
    db = _FakeDb()
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Failed to establish a connection to the primary server or any "
                            "backup address. [Errno 61] Connection refused")
        return db.connect(**kw)

    VerticaAdmin(_dsn(), connect=flaky).count_rows("T1")
    assert calls["n"] == 3


def test_open_raises_permanent_error_immediately():
    def bad(**kw):
        raise Exception('Severity: FATAL, Message: Authentication failed for username "qasource"')
    with pytest.raises(Exception, match="Authentication failed"):
        VerticaAdmin(_dsn(), connect=bad).count_rows("T1")


@pytest.mark.parametrize("role", ["source", "target"])
def test_run_sql_uses_configured_schema_when_existing_user_is_ready(role):
    # Existing users default to their same-named schemas, not this route override.
    dsn = {**_dsn(), f"{role}_schema": "route_data"}
    db = _FakeDb(reject={"dbadmin"})
    adm = VerticaAdmin(dsn, connect=db.connect, role=role)
    adm.ensure_setup()
    adm.run_sql("CREATE TABLE example (id INT);")
    assert _stmts(db, dsn[f"{role}_user"])[-2:] == [
        "SET SEARCH_PATH TO route_data, public", "CREATE TABLE example (id INT)"]
    assert not any(s.startswith("ALTER USER") for s in _stmts(db))


class _CatalogDb(_FakeDb):
    """Catalog lookups distinguish an exact name from LIKE's underscore wildcard."""

    def connect(self, **kw):
        conn = super().connect(**kw)
        cur = conn.cursor()
        execute = cur.execute
        rows = []

        def catalog_execute(sql, params=None):
            execute(sql, params)
            if params is not None:
                name = next(iter(params.values()))
                match = (lambda value: re.fullmatch(name.replace("_", "."), value, re.I)) \
                    if " ILIKE " in sql else (lambda value: value.lower() == name.lower())
                if "v_catalog.tables" in sql:
                    rows[:] = [("wanted",)] if match("QA_SOURCE") else []
                    if match("qaXsource"):
                        rows.append(("unrelated",))
                else:
                    rows[:] = [(1,)] if match("qaXsource") else []
            else:
                rows[:] = cur.fetchall()

        return types.SimpleNamespace(cursor=lambda: types.SimpleNamespace(
            execute=catalog_execute, fetchall=lambda: list(rows)), close=conn.close)


def test_user_check_does_not_match_an_underscore_lookalike():
    db = _CatalogDb(reject={"qa_source", "qatarget"})
    VerticaAdmin({**_dsn(), "source_user": "qa_source"}, connect=db.connect).ensure_setup()
    assert "CREATE USER qa_source IDENTIFIED BY 'striim'" in _stmts(db, "dbadmin")


def test_schema_check_does_not_match_an_underscore_lookalike():
    db = _CatalogDb()
    adm = VerticaAdmin({**_dsn(), "source_schema": "qa_source"}, connect=db.connect)
    assert not adm._ready("source")


def test_table_listing_does_not_match_an_underscore_lookalike():
    db = _CatalogDb()
    adm = VerticaAdmin({**_dsn(), "source_schema": "qa_source"}, connect=db.connect)
    assert adm._list_tables() == ["wanted"]
