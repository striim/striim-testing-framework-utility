"""What `run_sql_text` and `query_rows` send through each SQL driver, against recording fakes.

The connection, the statements and the commit each driver runner sends were exercised only by the
`docker`-marked suites, which the release gate (`-m "not docker"`) deselects. Each fake driver
module stands in for the real one in `sys.modules`; the runners import their driver lazily, so the
fake is the one they call, and nothing reaches a database.
"""
from __future__ import annotations

import sys
from types import ModuleType

import pytest

from inttest import dbroutes


class _Driver(ModuleType):
    """A driver module: `connect(**kw)` returns a connection whose cursor records each statement."""

    def __init__(self, name, fail=None, rows=((1, "a"),)):
        super().__init__(name)
        self.log, self.fail, self.rows = [], fail or (lambda sql: None), list(rows)

    def connect(self, **kw):
        self.log.append(("connect", kw))
        return _Conn(self)


class _Conn:
    def __init__(self, driver):
        self.d = driver

    def cursor(self):
        return _Cursor(self.d)

    def commit(self):
        self.d.log.append(("commit",))

    def close(self):
        self.d.log.append(("close",))


class _Cursor:
    def __init__(self, driver):
        self.d = driver

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.d.log.append(("execute", sql))
        error = self.d.fail(sql)
        if error:
            raise error

    def fetchall(self):
        return self.d.rows

    def close(self):
        pass


def _install(monkeypatch, name, **kw):
    driver = _Driver(name, **kw)
    monkeypatch.setitem(sys.modules, name, driver)
    return driver


def _tokens(service, **extra):
    up = service.upper()
    out = {f"{up}_HOST": "db.local", f"{up}_PORT": "15432", f"{up}_DB": "intdb",
           f"{up}_SOURCE_USER": "qasource", f"{up}_SOURCE_PASSWORD": "srcpw", f"{up}_SOURCE_SCHEMA": "qasource",
           f"{up}_TARGET_USER": "qatarget", f"{up}_TARGET_PASSWORD": "tgtpw", f"{up}_TARGET_SCHEMA": "qatarget",
           "TID": "t1a2b3c4d5_"}
    out.update(extra)
    return out


SQL = "CREATE TABLE ${TID}t (id int);\nINSERT INTO ${TID}t VALUES (1);\n"
STATEMENTS = ["CREATE TABLE t1a2b3c4d5_t (id int)", "INSERT INTO t1a2b3c4d5_t VALUES (1)"]


def test_postgres_sets_the_routes_schema_then_runs_each_statement_and_commits(monkeypatch):
    pg = _install(monkeypatch, "psycopg2")
    dbroutes.run_sql_text("postgres-target", SQL, _tokens("postgres"))
    assert pg.log == [
        ("connect", {"host": "db.local", "port": 15432, "dbname": "intdb", "user": "qatarget", "password": "tgtpw"}),
        ("execute", 'SET search_path TO "qatarget"'), *[("execute", s) for s in STATEMENTS], ("commit",), ("close",)]


def test_postgres_query_reads_one_statement_in_the_routes_schema(monkeypatch):
    pg = _install(monkeypatch, "psycopg2", rows=[(1, "a"), (2, "b")])
    rows = dbroutes.query_rows("postgres-source", "SELECT id, v FROM ${TID}t ORDER BY id;", _tokens("postgres"))
    assert rows == [[1, "a"], [2, "b"]]
    assert [e for e in pg.log if e[0] == "execute"] == [
        ("execute", 'SET search_path TO "qasource"'), ("execute", "SELECT id, v FROM t1a2b3c4d5_t ORDER BY id")]
    assert pg.log[-1] == ("close",)


def test_a_two_statement_query_is_refused_before_any_connection(monkeypatch):
    pg = _install(monkeypatch, "psycopg2")
    with pytest.raises(dbroutes.RouteError, match="SINGLE statement"):
        dbroutes.query_rows("postgres-source", "SELECT 1; SELECT 2", _tokens("postgres"))
    assert pg.log == []


def test_sql_that_renders_to_no_statement_opens_no_connection(monkeypatch):
    pg = _install(monkeypatch, "psycopg2")
    dbroutes.run_sql_text("postgres-source", "-- nothing here\n/* or here */\n", _tokens("postgres"))
    assert pg.log == []


def test_postgres_rejects_an_unsafe_schema_before_running_anything(monkeypatch):
    pg = _install(monkeypatch, "psycopg2")
    with pytest.raises(dbroutes.RouteError, match="unsafe schema identifier"):
        dbroutes.run_sql_text("postgres-source", SQL, _tokens("postgres", POSTGRES_SOURCE_SCHEMA='qa"; DROP'))
    assert [e for e in pg.log if e[0] == "execute"] == [] and pg.log[-1] == ("close",)


def test_oracle_drops_the_sqlplus_terminator_and_commits(monkeypatch):
    ora = _install(monkeypatch, "oracledb")
    params = dbroutes.connection_params("oracle-source", {
        "ORACLE_HOST": "db.local", "ORACLE_PORT": "11521", "ORACLE_SERVICE": "FREEPDB1",
        "ORACLE_SOURCE_USER": "qasource", "ORACLE_SOURCE_PASSWORD": "srcpw", "ORACLE_SOURCE_SCHEMA": "QASOURCE"})
    dbroutes._RUNNERS["oracle"](params, ["BEGIN NULL; END;\n/", "/", "DROP TABLE t"])
    assert ora.log == [("connect", {"user": "qasource", "password": "srcpw", "dsn": "db.local:11521/FREEPDB1"}),
                       ("execute", "BEGIN NULL; END;"), ("execute", "DROP TABLE t"), ("commit",), ("close",)]


@pytest.mark.parametrize("service", ["mysql", "mariadb"])
def test_mysql_connects_to_the_routes_schema_with_autocommit(monkeypatch, service):
    my = _install(monkeypatch, "pymysql")
    dbroutes.run_sql_text(f"{service}-source", SQL, _tokens(service))
    assert my.log == [
        ("connect", {"host": "db.local", "port": 15432, "database": "qasource", "user": "qasource",
                     "password": "srcpw", "autocommit": True}),
        *[("execute", s) for s in STATEMENTS], ("close",)]


def test_sqlserver_runs_with_autocommit_and_no_schema_statement(monkeypatch):
    ms = _install(monkeypatch, "pymssql")
    dbroutes.run_sql_text("sqlserver-target", SQL, _tokens("sqlserver"))
    assert ms.log == [
        ("connect", {"server": "db.local", "port": "15432", "database": "intdb", "user": "qatarget",
                     "password": "tgtpw", "autocommit": True}),
        *[("execute", s) for s in STATEMENTS], ("close",)]


def test_vertica_sets_the_search_path_first(monkeypatch):
    vt = _install(monkeypatch, "vertica_python")
    dbroutes.run_sql_text("vertica-target", SQL, _tokens("vertica"))
    assert vt.log[0] == ("connect", {"host": "db.local", "port": 15432, "database": "intdb", "user": "qatarget",
                                     "password": "tgtpw", "autocommit": True, "connection_timeout": 30})
    assert vt.log[1:] == [("execute", "SET SEARCH_PATH TO qatarget, public"),
                          *[("execute", s) for s in STATEMENTS], ("close",)]


def _teradata(monkeypatch, message):
    return _install(monkeypatch, "teradatasql",
                    fail=lambda sql: RuntimeError(message) if "missing" in sql else None)


def test_teradata_skips_only_a_drop_of_a_table_that_does_not_exist(monkeypatch):
    td = _teradata(monkeypatch, "[Session 12] [Teradata Database] [Error 3807] Object 'missing' does not exist.")
    dbroutes.run_sql_text("teradata-source", "DROP TABLE missing;\nCREATE TABLE t (id int);", _tokens("teradata"))
    assert td.log == [
        ("connect", {"host": "db.local", "dbs_port": "15432", "user": "qasource", "password": "srcpw", "tmode": "ANSI"}),
        ("execute", "DROP TABLE missing"), ("execute", "CREATE TABLE t (id int)"), ("close",)]


@pytest.mark.parametrize("sql, message", [
    # error 3807 from anything but a DROP is a real missing object
    ("INSERT INTO missing VALUES (1)", "[Session 12] [Teradata Database] [Error 3807] Object 'missing' does not exist."),
    # a DROP failing for another reason in session 3807 is not the skipped error
    ("DROP TABLE missing", "[Session 3807] [Teradata Database] [Error 3524] The user does not have DROP TABLE access."),
])
def test_teradata_raises_every_other_failure(monkeypatch, sql, message):
    td = _teradata(monkeypatch, message)
    with pytest.raises(RuntimeError, match="Teradata Database"):
        dbroutes.run_sql_text("teradata-source", f"{sql};\nCREATE TABLE t (id int);", _tokens("teradata"))
    assert ("execute", "CREATE TABLE t (id int)") not in td.log and td.log[-1] == ("close",)
