"""TeradataAdmin, hermetically: pytest's own connection to a Teradata target (setup, DDL/seed,
data and diff reads). The framework ships no Teradata service; this
admin serves a customer's own instance or a consumer service named `teradata`."""
import types

import pytest

from livetest.teradataadmin import TeradataAdmin


def _dsn():
    return {"host": "h", "port": 1025, "user": "dbc", "password": "dbc",
            "source_user": "qasource", "source_password": "striim", "source_schema": "qasource",
            "target_user": "qatarget", "target_password": "striim", "target_schema": "qatarget"}


_VIEWS = "SELECT TRIM(TableName) FROM DBC.TablesV WHERE DatabaseName = ? AND TableKind IN ('V')"
_TABLES = "SELECT TRIM(TableName) FROM DBC.TablesV WHERE DatabaseName = ? AND TableKind IN ('T', 'O')"


class _FakeDb:
    """A teradatasql-like connect() recording (user, sql, params); `results` maps a SQL
    prefix to the rows a query returns, `fail` is a set of statements that raise."""

    def __init__(self, results=None, fail=(), reject=()):
        self.executed = []
        self.logins = []
        self.results = results or {}
        self.fail = set(fail)
        self.reject = set(reject)          # users whose logon fails (missing user)

    def connect(self, host, port, user, password):
        self.logins.append(user)
        if user in self.reject:
            raise Exception("[Error 8017] The UserId, Password or Account is invalid.")
        rows = []

        def execute(sql, params=None):
            self.executed.append((user, sql, params))
            if sql in self.fail:
                raise Exception(f"[Error 5708] cannot drop {sql}")
            rows[:] = next((v(params) if callable(v) else v
                            for k, v in self.results.items() if sql.startswith(k)), [(1,)])

        cur = types.SimpleNamespace(execute=execute, fetchall=lambda: list(rows),
                                    description=[("A",), ("B",)])
        return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)


def test_ensure_setup_creates_only_missing_users():
    db = _FakeDb(results={"SELECT 1 FROM DBC.DatabasesV": []}, reject={"qatarget"})
    TeradataAdmin(_dsn(), connect=db.connect).ensure_setup()
    creates = [(u, sql) for u, sql, _ in db.executed if sql.startswith("CREATE USER")]
    assert creates == [("dbc", 'CREATE USER qatarget FROM dbc AS PERM = 10e9, SPOOL = 10e9, PASSWORD = "striim"')]

def test_ensure_setup_needs_no_admin_when_roles_log_in():
    # a live instance may not hand out admin credentials; existing users need nothing
    db = _FakeDb(reject={"dbc"})
    TeradataAdmin(_dsn(), connect=db.connect).ensure_setup()
    assert "dbc" not in db.logins and not any(sql.startswith("CREATE") for _, sql, _ in db.executed)

def test_ensure_setup_rejects_unsafe_password_when_it_must_create():
    dsn = {**_dsn(), "target_password": "x; DROP USER dbc"}
    db = _FakeDb(results={"SELECT 1 FROM DBC.DatabasesV": []}, reject={"qasource", "qatarget"})
    with pytest.raises(ValueError, match="qatarget"):
        TeradataAdmin(dsn, connect=db.connect).ensure_setup()
    assert not any(sql.startswith("CREATE USER qatarget") for _, sql, _ in db.executed)

def test_ensure_setup_accepts_any_password_for_existing_users():
    # a live instance's existing users may have any password; nothing is inlined then
    dsn = {**_dsn(), "source_password": "P@ss-1", "target_password": "x y"}
    db = _FakeDb(results={"SELECT 1 FROM DBC.DatabasesV": [(1,)]}, reject={"qasource"})
    TeradataAdmin(dsn, connect=db.connect).ensure_setup()
    assert not any(sql.startswith("CREATE USER") for _, sql, _ in db.executed)

def test_run_sql_splits_statements_as_role_user():
    db = _FakeDb()
    TeradataAdmin(_dsn(), connect=db.connect, role="target").run_sql(
        "-- comment\nCREATE TABLE qatarget.T1 (a INT);\nINSERT INTO qatarget.T1 VALUES (1);\n")
    stmts = [sql for _, sql, _ in db.executed if sql != "SELECT 1"]
    assert stmts == ["CREATE TABLE qatarget.T1 (a INT)", "INSERT INTO qatarget.T1 VALUES (1)"]
    assert set(db.logins) == {"qatarget"}

def test_drop_test_tables_prefix_is_case_insensitive_and_retries_fk_order():
    listing = [("t1abc_ORDERS",), ("T1ABC_ITEMS",), ("OTHER",)]
    db = _FakeDb(results={_VIEWS: [], _TABLES: listing},
                 fail={"DROP TABLE qasource.t1abc_ORDERS"})
    adm = TeradataAdmin(_dsn(), connect=db.connect)
    # a table that will not drop is reported, so teardown can warn instead of passing quietly
    with pytest.raises(RuntimeError, match="t1abc_ORDERS"):
        adm.drop_test_tables("T1ABC_")
    drops = [sql for _, sql, _ in db.executed if sql.startswith("DROP")]
    # ORDERS fails, ITEMS drops, then a second pass retries ORDERS once more and stops
    assert drops == ["DROP TABLE qasource.t1abc_ORDERS", "DROP TABLE qasource.T1ABC_ITEMS",
                     "DROP TABLE qasource.t1abc_ORDERS"]
    listed = [params for _, sql, params in db.executed if sql.startswith("SELECT TRIM")]
    assert listed == [["qasource"], ["qasource"]]      # views, then tables

def test_drop_test_tables_serial_drops_everything():
    db = _FakeDb(results={_VIEWS: [], _TABLES: [("A1",), ("B2",)]})
    TeradataAdmin(_dsn(), connect=db.connect, role="target").drop_test_tables("")
    drops = [sql for _, sql, _ in db.executed if sql.startswith("DROP")]
    assert drops == ["DROP TABLE qatarget.A1", "DROP TABLE qatarget.B2"]

def test_admin_role_runs_sql_as_dbc():
    db = _FakeDb()
    TeradataAdmin(_dsn(), connect=db.connect, role="admin").run_sql(
        "SELECT SYSLIB.AbortSessions(HostNo, UserName, SessionNo, 'Y', 'Y') FROM DBC.SessionInfoV;")
    assert set(db.logins) == {"dbc"}
    assert [sql for _, sql, _ in db.executed if sql != "SELECT 1"] == [
        "SELECT SYSLIB.AbortSessions(HostNo, UserName, SessionNo, 'Y', 'Y') FROM DBC.SessionInfoV"]

def test_admin_role_takes_its_credentials_from_the_dsn():
    db = _FakeDb()
    dsn = {**_dsn(), "user": "tdadmin", "password": "pw"}
    TeradataAdmin(dsn, connect=db.connect, role="admin").run_sql("SELECT 2;")
    assert set(db.logins) == {"tdadmin"}

@pytest.mark.parametrize("prefix", ["", "T1ABC_"])
def test_admin_role_drop_is_a_no_op(prefix):
    # the serial sweep would otherwise list and drop every table in dbc
    db = _FakeDb(results={"SELECT TRIM(TableName)": [("A1",)]})
    TeradataAdmin(_dsn(), connect=db.connect, role="admin").drop_test_tables(prefix)
    assert db.logins == [] and db.executed == []

def test_qualified_defaults_to_role_database_and_rejects_unsafe():
    adm = TeradataAdmin(_dsn(), connect=_FakeDb().connect, role="target")
    assert adm._qualified("T1") == "qatarget.T1"
    assert adm._qualified("qasource.T1") == "qasource.T1"
    with pytest.raises(ValueError):
        adm._qualified("qasource.T1; DROP TABLE x")

def test_count_and_select_rows():
    db = _FakeDb(results={"LOCKING TABLE qasource.T1 FOR ACCESS SELECT COUNT": [(3,)],
                          "LOCKING TABLE qasource.T1 FOR READ NOWAIT SELECT * FROM qasource.T1":
                              [(1, None), (2, "x")]})
    adm = TeradataAdmin(_dsn(), connect=db.connect)
    assert adm.count_rows("T1") == 3
    assert adm.select_rows("qasource.T1") == [{"A": "1", "B": None}, {"A": "2", "B": "x"}]
    # the poll-time count must not queue behind (or deadlock with) a writer's WRITE lock
    assert ("qasource", "LOCKING TABLE qasource.T1 FOR ACCESS SELECT COUNT(*) FROM qasource.T1",
            None) in db.executed

def test_count_rows_committed_uses_nowait_read_lock():
    db = _FakeDb(results={"LOCKING TABLE": [(5,)]})
    assert TeradataAdmin(_dsn(), connect=db.connect).count_rows_committed("T1") == 5
    assert any(sql == "LOCKING TABLE qasource.T1 FOR READ NOWAIT SELECT COUNT(*) FROM qasource.T1"
               for _, sql, _ in db.executed)

# ---- availability -----------------------------------------------------------


def test_open_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr("livetest.teradataadmin.time.sleep", lambda *_: None)
    db = _FakeDb()
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("dial tcp 127.0.0.1:1025: connect: connection refused")
        return db.connect(**kw)

    TeradataAdmin(_dsn(), connect=flaky).count_rows("T1")
    assert calls["n"] == 3

def test_open_raises_permanent_error_immediately():
    def bad(**kw):
        raise Exception("[Error 8017] The UserId, Password or Account is invalid.")
    with pytest.raises(Exception, match="8017"):
        TeradataAdmin(_dsn(), connect=bad).count_rows("T1")



# ---- STATUS 35 (1): the committed read must not queue behind a writer ---------------------

_LOCKED = ("[Session 1234] [Teradata Database] [Error 7423] Object already locked and NOWAIT. "
           "Transaction Aborted.")


class _WriterHoldsTarget:
    """A teradatasql-like connect() where a writer's open batch holds every qatarget table: a
    plain SELECT waits `block` seconds for its commit, a NOWAIT read is refused at once (7423).
    qasource is free."""

    def __init__(self, block=2.0):
        self.block = block
        self.executed = []

    def connect(self, host, port, user, password):
        rows = []

        def execute(sql, params=None):
            self.executed.append(sql)
            if "qatarget." in sql and "FOR READ NOWAIT" in sql:
                raise Exception(_LOCKED)
            if sql.startswith("SELECT * FROM qatarget."):
                import time as _t
                _t.sleep(self.block)
                rows[:] = []
                return
            rows[:] = [("1",)] if sql != "SELECT 1" else [(1,)]

        cur = types.SimpleNamespace(execute=execute, fetchall=lambda: list(rows),
                                    description=[("ID",)])
        return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)


def test_diff_poll_times_out_while_a_writer_holds_the_target():
    # The gap: a plain SELECT waits for the writer's commit, so a poll overruns its timeout.
    import time
    from livetest.assertions import AssertionFailed
    from livetest.assertions.diff import assert_diff
    db = _WriterHoldsTarget(block=2.0)
    admins = {"teradata-source": TeradataAdmin(_dsn(), connect=db.connect, role="source"),
              "teradata-target": TeradataAdmin(_dsn(), connect=db.connect, role="target")}
    spec = {"source": "qasource.T1", "source_db": "teradata-source",
            "target": "qatarget.T1", "target_db": "teradata-target"}
    began = time.monotonic()
    with pytest.raises(AssertionFailed, match="not readable yet"):
        assert_diff(admins, [spec], timeout=0.5, poll=0.05)
    assert time.monotonic() - began < 1.5, "the poll waited behind the writer's lock"


def test_data_match_poll_treats_a_locked_target_as_not_yet():
    from livetest.assertions import NotYetReadable
    from livetest.assertions.data import _evaluate
    db = _WriterHoldsTarget()
    adm = TeradataAdmin(_dsn(), connect=db.connect, role="target")
    with pytest.raises(NotYetReadable):
        adm.select_rows("T1")
    ok, detail, _, actual = _evaluate(adm, {"target": "qatarget.T1"}, [{"ID": "1"}],
                                      {frozenset({("ID", "1")})})
    assert not ok and "not readable yet" in detail and actual is None


def test_select_rows_raises_other_errors_unchanged():
    def bad(**kw):
        conn = _FakeDb().connect(**kw)
        cur = conn.cursor()
        orig = cur.execute

        def execute(sql, params=None):
            if sql.startswith("LOCKING TABLE"):
                raise Exception("[Session 7423] [Teradata Database] [Error 3807] Object 'T1' does not exist.")
            return orig(sql, params)
        cur.execute = execute
        return conn
    with pytest.raises(Exception, match="3807"):
        TeradataAdmin(_dsn(), connect=bad).select_rows("T1")


# ---- STATUS 35 (3): teardown drops this test's views too ----------------------------------


def test_drop_test_tables_drops_views_before_tables():
    db = _FakeDb(results={_VIEWS: [("t1abc_resv_v",), ("T1ABC_RESV_CHK",), ("other_v",)],
                          _TABLES: [("t1abc_resv_base",), ("OTHER",)]})
    TeradataAdmin(_dsn(), connect=db.connect, role="target").drop_test_tables("T1ABC_")
    drops = [sql for _, sql, _ in db.executed if sql.startswith("DROP")]
    assert drops == ["DROP VIEW qatarget.t1abc_resv_v", "DROP VIEW qatarget.T1ABC_RESV_CHK",
                     "DROP TABLE qatarget.t1abc_resv_base"]


def test_drop_test_tables_retries_a_view_over_a_view_and_names_one_that_will_not_drop():
    db = _FakeDb(results={_VIEWS: [("t1abc_a",), ("t1abc_b",)], _TABLES: []},
                 fail={"DROP VIEW qatarget.t1abc_a"})
    with pytest.raises(RuntimeError, match="views: t1abc_a"):
        TeradataAdmin(_dsn(), connect=db.connect, role="target").drop_test_tables("t1abc_")
    drops = [sql for _, sql, _ in db.executed if sql.startswith("DROP")]
    assert drops == ["DROP VIEW qatarget.t1abc_a", "DROP VIEW qatarget.t1abc_b",
                     "DROP VIEW qatarget.t1abc_a"]


def test_committed_count_refused_by_a_writer_lock_is_not_yet():
    from livetest.assertions import NotYetReadable
    adm = TeradataAdmin(_dsn(), connect=_WriterHoldsTarget().connect, role="target")
    with pytest.raises(NotYetReadable, match="locked by a writer"):
        adm.count_rows_committed("T1")
