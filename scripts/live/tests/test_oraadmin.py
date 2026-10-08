import pytest
from livetest.oraadmin import OraAdmin, _split_statements

class FakeCursor:
    def __init__(self, log): self.log = log; self.description = None
    def execute(self, sql): self.log.append(sql)
    def close(self): pass

class FakeConn:
    def __init__(self, log):
        self.log = log; self.autocommit = False; self.closed = False
    def cursor(self): return FakeCursor(self.log)
    def close(self): self.closed = True

def _admin(log):
    return OraAdmin({"host": "h", "port": 1521, "service": "FREEPDB1", "source_user": "u", "source_password": "p"},
                    connect=lambda **kw: FakeConn(log))

# ---- statement splitting ----------------------------------------------------

def test_split_drops_comments_and_terminators():
    sql = """
    -- a comment
    CREATE TABLE QASOURCE.CUSTOMERS (ID NUMBER PRIMARY KEY);
    INSERT INTO QASOURCE.CUSTOMERS VALUES (1);
    """
    stmts = _split_statements(sql)
    assert stmts == [
        "CREATE TABLE QASOURCE.CUSTOMERS (ID NUMBER PRIMARY KEY)",
        "INSERT INTO QASOURCE.CUSTOMERS VALUES (1)",
    ]

def test_split_ignores_blank_and_slash_only():
    assert _split_statements(";\n/\n   ;  ") == []

def test_run_sql_executes_each_statement_autocommit():
    log = []
    conn_holder = {}
    def connect(**kw):
        c = FakeConn(log); conn_holder["c"] = c; return c
    admin = OraAdmin({"host":"h","port":1521,"service":"FREEPDB1","source_user":"u","source_password":"p"},
                     connect=connect)
    admin.run_sql("CREATE TABLE T (a NUMBER); INSERT INTO T VALUES (1);")
    # _open probes with "SELECT 1 FROM dual" (DB-open check) before returning the conn.
    stmts = [s for s in log if s != "SELECT 1 FROM dual"]
    assert stmts == ["CREATE TABLE T (a NUMBER)", "INSERT INTO T VALUES (1)"]
    assert conn_holder["c"].autocommit is True
    assert conn_holder["c"].closed is True

# ---- reads ------------------------------------------------------------------

class FakeCursorRes:
    def __init__(self, rows, description):
        self._rows = rows; self.description = description; self.executed = []
    def execute(self, sql): self.executed.append(sql)
    def fetchall(self): return self._rows
    def close(self): pass

class FakeConnRes:
    def __init__(self, rows, description):
        self._rows = rows; self._desc = description; self.autocommit = False
    def cursor(self): return FakeCursorRes(self._rows, self._desc)
    def close(self): pass

def test_count_rows_returns_int():
    admin = OraAdmin({"host":"h","port":1,"service":"S","source_user":"u","source_password":"p"},
                     connect=lambda **kw: FakeConnRes([(7,)], [("COUNT(*)",)]))
    assert admin.count_rows("QATARGET.USERS_INFO") == 7

def test_select_rows_returns_str_coerced_dicts():
    admin = OraAdmin({"host":"h","port":1,"service":"S","source_user":"u","source_password":"p"},
                     connect=lambda **kw: FakeConnRes([(1, "Jane", None)],
                                                      [("ID",), ("FIRST_NAME",), ("LAST_NAME",)]))
    rows = admin.select_rows("QATARGET.CUSTOMERS")
    assert rows == [{"ID": "1", "FIRST_NAME": "Jane", "LAST_NAME": None}]

def test_qualified_requires_schema_dot_table():
    admin = _admin([])
    with pytest.raises(ValueError, match="schema.table"):
        admin.count_rows("USERS_INFO")

def test_read_rejects_bad_identifier():
    admin = OraAdmin({"host":"h","port":1,"service":"S","source_user":"u","source_password":"p"},
                     connect=lambda **kw: FakeConnRes([(0,)], [("COUNT(*)",)]))
    with pytest.raises(ValueError):
        admin.count_rows("QATARGET.tbl; DROP")


# ---- drop_test_tables (teardown cleanup) ------------------------------------

class FakeDropCursor:
    def __init__(self, state):
        self.state = state; self.description = [("TABLE_NAME",)]
    def execute(self, sql):
        self.state["executed"].append(sql)
        if sql.startswith("DROP TABLE"):
            name = sql.split()[2]
            if name in self.state["fail"]:
                raise RuntimeError("ORA-02449: unique/primary keys referenced")
            self.state["tables"] = [t for t in self.state["tables"] if t[0] != name]
    def fetchall(self):
        return list(self.state["tables"])
    def close(self): pass

class FakeDropConn:
    def __init__(self, state): self.state = state; self.autocommit = False
    def cursor(self): return FakeDropCursor(self.state)
    def close(self): pass

def _drop_admin(state):
    return OraAdmin({"host": "h", "port": 1521, "service": "S",
                     "source_user": "u", "source_password": "p"},
                    connect=lambda **kw: FakeDropConn(state))

def test_drop_test_tables_drops_every_user_table_when_no_prefix():
    state = {"tables": [("T1",), ("T2",)], "executed": [], "fail": set()}
    _drop_admin(state).drop_test_tables()
    drops = [s for s in state["executed"] if s.startswith("DROP TABLE")]
    assert drops == ["DROP TABLE T1 CASCADE CONSTRAINTS PURGE",
                     "DROP TABLE T2 CASCADE CONSTRAINTS PURGE"]
    assert state["tables"] == []

def test_drop_test_tables_prefix_filters_and_a_failed_drop_does_not_raise():
    state = {"tables": [("TABC_ORDERS",), ("OTHER",)], "executed": [],
             "fail": {"TABC_ORDERS"}}
    _drop_admin(state).drop_test_tables("TABC_")   # must not raise
    drops = [s for s in state["executed"] if s.startswith("DROP TABLE")]
    assert drops == ["DROP TABLE TABC_ORDERS CASCADE CONSTRAINTS PURGE"]   # OTHER untouched
