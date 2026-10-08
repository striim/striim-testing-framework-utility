import re
import pytest
from google.cloud.spanner_v1.data_types import JsonObject
from livetest.spanneradmin import SpannerAdmin, _split_ddl, _check_table, _jsonify

# ---- pure helpers -----------------------------------------------------------

def test_split_ddl_drops_comments_and_splits():
    sql = """
    -- a comment
    CREATE TABLE emp (id STRING(36)) PRIMARY KEY (id);
    CREATE TABLE dep (id STRING(36)) PRIMARY KEY (id);
    """
    assert _split_ddl(sql) == [
        "CREATE TABLE emp (id STRING(36)) PRIMARY KEY (id)",
        "CREATE TABLE dep (id STRING(36)) PRIMARY KEY (id)",
    ]

def test_check_table_allows_one_or_two_parts():
    assert _check_table("emp") == "emp"
    assert _check_table("public.emp") == "public.emp"

def test_check_table_rejects_injection():
    with pytest.raises(ValueError):
        _check_table("emp; DROP TABLE x")
    with pytest.raises(ValueError):
        _check_table("a.b.c")

# ---- admin with an injected fake database -----------------------------------

class _Field:
    def __init__(self, name): self.name = name

class _ResultSet:
    def __init__(self, rows, fields):
        self._rows = rows
        self.fields = [_Field(f) for f in fields]
    def __iter__(self): return iter(self._rows)

class _Snapshot:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute_sql(self, sql):
        table = re.search(r"FROM\s+([\w.]+)", sql).group(1)
        fields, rows = self.db.tables[table]
        if "count(*)" in sql.lower():
            return _ResultSet([[len(rows)]], ["c"])
        return _ResultSet(rows, fields)

class _Op:
    def result(self, *a): pass

class _FakeDatabase:
    def __init__(self, tables): self.tables = tables; self.ddl = []
    def snapshot(self): return _Snapshot(self)
    def update_ddl(self, stmts): self.ddl.extend(stmts); return _Op()

def _admin(db):
    return SpannerAdmin({"project": "p", "instance": "i", "database": "d",
                         "dialect": "google_standard_sql", "emulator_host": "localhost:9010"},
                        database=db)

def test_run_sql_pushes_split_statements_to_update_ddl():
    db = _FakeDatabase({})
    _admin(db).run_sql("CREATE TABLE a (id STRING(1)) PRIMARY KEY (id); CREATE TABLE b (id STRING(1)) PRIMARY KEY (id);")
    assert db.ddl == ["CREATE TABLE a (id STRING(1)) PRIMARY KEY (id)",
                      "CREATE TABLE b (id STRING(1)) PRIMARY KEY (id)"]

def test_select_rows_str_coerces():
    db = _FakeDatabase({"emp": (["id", "msg"], [[1, "a"], [2, None]])})
    assert _admin(db).select_rows("emp") == [{"id": "1", "msg": "a"}, {"id": "2", "msg": None}]

def test_count_rows_returns_int():
    db = _FakeDatabase({"emp": (["id"], [[1], [2], [3]])})
    assert _admin(db).count_rows("emp") == 3

def test_select_rows_rejects_bad_table():
    with pytest.raises(ValueError):
        _admin(_FakeDatabase({})).select_rows("emp; DROP TABLE x")

# ---- _jsonify: normalize one column value regardless of Spanner column type -------

def test_jsonify_handles_json_and_native_shapes():
    # JSON object column -> plain dict
    assert _jsonify(JsonObject({"BUG_ID": "1", "BUG_NAME": "Bug 1"})) == {"BUG_ID": "1", "BUG_NAME": "Bug 1"}
    # top-level JSON ARRAY column -> plain list (must NOT collapse to {})
    assert _jsonify(JsonObject([{"x": 1}, {"y": 2}])) == [{"x": 1}, {"y": 2}]
    # SQL NULL JSON -> None
    assert _jsonify(JsonObject()) is None
    # native ARRAY<STRING> / ARRAY<INT64> come back as plain lists -> pass through
    assert _jsonify(["Bug 1a", "Bug 1b"]) == ["Bug 1a", "Bug 1b"]
    assert _jsonify([11, 21]) == [11, 21]
    # plain NULL
    assert _jsonify(None) is None

def test_select_json_rows_mixed_columns():
    # bug 1: JSON object, bug 2: top-level JSON array, bug 3: JSON null, bug 4: native array
    db = _FakeDatabase({"bug_table": (["bug_id", "col"], [
        [1, JsonObject({"BUG_ID": "1"})],
        [2, JsonObject([{"BUG_ID": "2"}])],
        [3, JsonObject()],
        [4, ["a", "b"]],
    ])})
    got = dict(_admin(db).select_json_rows("bug_table", ["bug_id"], "col"))
    assert got[("1",)] == {"BUG_ID": "1"}
    assert got[("2",)] == [{"BUG_ID": "2"}]
    assert got[("3",)] is None
    assert got[("4",)] == ["a", "b"]


# ---- drop_test_tables (teardown cleanup) ------------------------------------

_INFO = "information_schema.tables"

def test_drop_test_tables_drops_user_tables_with_prefix():
    db = _FakeDatabase({_INFO: (["table_name"], [["tabc_bug"], ["other"]])})
    _admin(db).drop_test_tables("tabc_")
    assert db.ddl == ["DROP TABLE tabc_bug"]      # non-matching table untouched

def test_drop_test_tables_drops_all_when_no_prefix():
    db = _FakeDatabase({_INFO: (["table_name"], [["a"], ["b"]])})
    _admin(db).drop_test_tables()
    assert db.ddl == ["DROP TABLE a", "DROP TABLE b"]

def test_drop_test_tables_terminates_and_swallows_when_drops_keep_failing():
    class _FailingDB(_FakeDatabase):
        def update_ddl(self, stmts):
            raise RuntimeError("cannot drop")
    db = _FailingDB({_INFO: (["table_name"], [["a"], ["b"]])})
    _admin(db).drop_test_tables()   # must not raise, must not loop forever
    assert db.ddl == []


# ---- drop_test_change_streams (teardown cleanup) ----------------------------
# A change stream is a CAPPED resource: Spanner allows at most 3 tracking the same table
# (or ALL). The change-stream reader cases each ship `CREATE CHANGE STREAM IF NOT EXISTS
# ${TID}SltCdcReaderStream FOR ALL` in their `ddl:`, teardown dropped only tables, and
# three runs' leftovers wedged every later run with "not allowed to have more than 3
# Change Streams tracking the same table or non-key column or ALL: ALL".

_STREAMS = "information_schema.change_streams"

def test_drop_test_change_streams_drops_only_this_tests_streams():
    db = _FakeDatabase({_STREAMS: (["change_stream_name"],
                                   [["tabc_SltCdcReaderStream"], ["tzzz_SltCdcReaderStream"]])})
    _admin(db).drop_test_change_streams("tabc_")
    assert db.ddl == ["DROP CHANGE STREAM tabc_SltCdcReaderStream"]  # sibling's stream survives

def test_drop_test_change_streams_drops_all_when_no_prefix():
    db = _FakeDatabase({_STREAMS: (["change_stream_name"], [["a"], ["b"]])})
    _admin(db).drop_test_change_streams()
    assert db.ddl == ["DROP CHANGE STREAM a", "DROP CHANGE STREAM b"]

def test_drop_test_tables_drops_the_streams_first():
    """plugin.py's teardown reaches every admin through drop_test_tables alone, so the
    stream cleanup only runs at all if it hangs off that call -- and it must come first,
    because a stream tracking a table explicitly blocks the table's drop."""
    db = _FakeDatabase({_INFO: (["table_name"], [["tabc_bug"]]),
                        _STREAMS: (["change_stream_name"], [["tabc_SltCdcReaderStream"]])})
    _admin(db).drop_test_tables("tabc_")
    assert db.ddl == ["DROP CHANGE STREAM tabc_SltCdcReaderStream", "DROP TABLE tabc_bug"]

def test_drop_test_change_streams_survives_a_database_without_the_view():
    """Best-effort: an emulator or dialect that cannot answer the change_streams query
    must not turn teardown into the run's reported failure."""
    class _NoView(_FakeDatabase):
        def snapshot(self): raise RuntimeError("no such table: information_schema.change_streams")
    db = _NoView({})
    _admin(db).drop_test_change_streams("tabc_")   # must not raise
    assert db.ddl == []

def test_a_failed_stream_drop_is_raised_after_trying_every_stream_and_the_tables():
    """A leaked stream holds one of the three slots, so a failed DROP must reach the teardown's
    cleanup-failure accounting instead of reading as clean."""
    class _StuckStream(_FakeDatabase):
        def update_ddl(self, stmts):
            if stmts == ["DROP CHANGE STREAM tabc_a"]:
                raise RuntimeError("stream busy")
            return super().update_ddl(stmts)
    db = _StuckStream({_INFO: (["table_name"], [["tabc_bug"]]),
                       _STREAMS: (["change_stream_name"], [["tabc_a"], ["tabc_b"]])})
    with pytest.raises(RuntimeError, match="tabc_a"):
        _admin(db).drop_test_tables("tabc_")
    assert db.ddl == ["DROP CHANGE STREAM tabc_b", "DROP TABLE tabc_bug"]


def test_a_table_listing_error_does_not_hide_a_failed_stream_drop():
    class _Both(_FakeDatabase):
        def update_ddl(self, stmts):
            raise RuntimeError("stream busy")
        def snapshot(self):
            if self.listed:
                raise RuntimeError("tables view gone")
            self.listed = True
            return super().snapshot()
    db = _Both({_STREAMS: (["change_stream_name"], [["tabc_a"]])})
    db.listed = False
    with pytest.raises(RuntimeError, match="tables view gone.*tabc_a"):
        _admin(db).drop_test_tables("tabc_")
