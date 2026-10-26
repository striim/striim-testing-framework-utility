import pytest
from decimal import Decimal
from types import SimpleNamespace
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


# ---- select_rows reads NUMBER exactly and LOBs while connected -------------------------------

class _DriverCursor(FakeCursorRes):
    """Applies outputtypehandler as python-oracledb does. By default NUMBER(p,0) and an
    unconstrained NUMBER fetch an integral value as int, everything else as float; a LOB is a
    locator that cannot be read after close."""
    def __init__(self, conn, columns):
        super().__init__([], [(f"C{i}", c) for i, c in enumerate(columns)])
        self.conn, self.arraysize, self.outputtypehandler = conn, 100, None

    def var(self, typ, arraysize=1, outconverter=None):
        return typ, outconverter

    def fetchall(self):
        import oracledb
        row = []
        for _, (typ, precision, scale, value) in self.description:
            meta = SimpleNamespace(type_code=typ, precision=precision, scale=scale)
            want = self.outputtypehandler(self, meta) if self.outputtypehandler else None
            if typ is oracledb.DB_TYPE_NUMBER:
                if want:
                    row.append(want[1](Decimal(value)))
                else:
                    as_int = scale == 0 or (scale == -127 and not precision)
                    row.append(int(Decimal(value)) if as_int and Decimal(value) == Decimal(value).to_integral_value()
                               else float(value))
            elif want is None:
                row.append(_Lob(self.conn, value))
            else:
                row.append(value)
        return [tuple(row)]


class _Lob:
    def __init__(self, conn, value): self.conn, self.value = conn, value
    def __str__(self):
        if self.conn.closed:
            raise RuntimeError("DPY-1001: not connected to database")
        return self.value


class _DriverConn:
    def __init__(self, columns): self.columns, self.closed = columns, False
    def cursor(self): return _DriverCursor(self, self.columns)
    def close(self): self.closed = True


def _read(columns):
    admin = OraAdmin({"host": "h", "port": 1, "service": "S", "source_user": "u", "source_password": "p"},
                     connect=lambda **kw: _DriverConn(columns))
    return list(admin.select_rows("QATARGET.T")[0].values())


def test_select_rows_keeps_number_digits_a_float_would_drop():
    oracledb = pytest.importorskip("oracledb")
    n = oracledb.DB_TYPE_NUMBER
    assert _read([(n, 20, 2, "12345678901234567.89"), (n, 20, 2, "12345678901234567.88")]) == \
        ["12345678901234567.89", "12345678901234567.88"]
    assert _read([(n, 0, -127, "0.1234567890123456789")]) == ["0.1234567890123456789"]


@pytest.mark.parametrize("precision, scale, value, text", [
    (10, 2, "42", "42.0"),          # a whole number at scale > 0 keeps the float spelling goldens use
    (10, 2, "42.5", "42.5"),
    (10, 6, "0.00001", "1e-05"),    # small values keep the float exponent spelling
    (0, -127, "0.0000001", "1e-07"),
    (0, -127, "7", "7"),            # unconstrained NUMBER: integral is an int
    (0, -127, "1.5", "1.5"),
    (10, 0, "123", "123"),
    (126, -127, "3", "3.0"),        # FLOAT
])
def test_select_rows_keeps_the_drivers_number_spelling(precision, scale, value, text):
    oracledb = pytest.importorskip("oracledb")
    assert _read([(oracledb.DB_TYPE_NUMBER, precision, scale, value)]) == [text]


def test_select_rows_reads_lobs_before_close():
    oracledb = pytest.importorskip("oracledb")
    assert _read([(oracledb.DB_TYPE_CLOB, 0, 0, "long text"), (oracledb.DB_TYPE_BLOB, 0, 0, b"\x00\xff")]) == \
        ["long text", "00ff"]
