import types

import pytest

from livetest.mssqladmin import MssqlAdmin


def _fake_connect_factory(state, executed):
    """A pymssql-like connect() backed by mutable `state['pw']` (the sa login's current
    password). A connect whose password != state['pw'] raises a 'Login failed' error
    (which MssqlAdmin treats as transient); an ALTER LOGIN seen on any cursor mutates
    state['pw'] so a subsequent connect with the new password succeeds."""
    def connect(server, port, user, password, database, autocommit):
        if password != state["pw"]:
            raise Exception("Login failed for user 'sa'.")

        def execute(sql, *args):
            executed.append(sql)
            if "ALTER LOGIN" in sql and "'striim'" in sql:
                state["pw"] = "striim"

        cur = types.SimpleNamespace(execute=execute, fetchall=lambda: [(1,)], close=lambda: None)
        return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    return connect


def _dsn(password="striim"):
    return {"host": "h", "port": 1433, "user": "sa", "password": password, "database": "qauser"}


def test_ensure_sa_password_migrates_from_bootstrap(monkeypatch):
    monkeypatch.setattr("livetest.mssqladmin.time.sleep", lambda *_: None)  # no real retry waits
    state = {"pw": "QAtestuser1"}       # container boots on the complex bootstrap password
    executed = []
    adm = MssqlAdmin(_dsn("striim"), connect=_fake_connect_factory(state, executed))

    adm._ensure_sa_password()

    assert state["pw"] == "striim"      # sa relaxed to the normalized password
    assert any("ALTER LOGIN [sa]" in s and "CHECK_POLICY = OFF" in s
               and "CHECK_EXPIRATION = OFF" in s and "'striim'" in s for s in executed)


def test_ensure_sa_password_is_noop_when_already_migrated(monkeypatch):
    monkeypatch.setattr("livetest.mssqladmin.time.sleep", lambda *_: None)
    state = {"pw": "striim"}            # sa already at the target (container re-resolved)
    executed = []
    adm = MssqlAdmin(_dsn("striim"), connect=_fake_connect_factory(state, executed))

    adm._ensure_sa_password()

    assert not any("ALTER LOGIN" in s for s in executed)   # no re-migration


def test_ensure_sa_password_noop_when_target_equals_bootstrap(monkeypatch):
    # If the configured password already meets complexity (== bootstrap), do nothing —
    # not even a probe connection.
    monkeypatch.setattr("livetest.mssqladmin.time.sleep", lambda *_: None)
    calls = []
    adm = MssqlAdmin(_dsn("QAtestuser1"),
                     connect=lambda **k: calls.append(k) or types.SimpleNamespace(
                         cursor=lambda: types.SimpleNamespace(
                             execute=lambda *a: None, fetchall=lambda: [(1,)], close=lambda: None),
                         close=lambda: None))

    adm._ensure_sa_password()

    assert calls == []                 # never opened a connection


def test_ensure_sa_password_rejects_unsafe_target(monkeypatch):
    monkeypatch.setattr("livetest.mssqladmin.time.sleep", lambda *_: None)
    state = {"pw": "QAtestuser1"}
    executed = []
    adm = MssqlAdmin(_dsn("striim'; DROP DATABASE x--"),
                     connect=_fake_connect_factory(state, executed))

    with pytest.raises(ValueError):
        adm._ensure_sa_password()
    assert not any("ALTER LOGIN" in s for s in executed)   # never issued the DDL


# ---- drop_test_tables (teardown cleanup) ------------------------------------

def test_drop_test_tables_prefix_filtered_multipass(monkeypatch):
    monkeypatch.setattr("livetest.mssqladmin.time.sleep", lambda *_: None)
    executed = []
    state = {"tables": ["tabc_parent", "tabc_child", "other"]}

    def connect(server, port, user, password, database, autocommit):
        def execute(sql, *args):
            executed.append(sql)
            if sql.startswith("DROP TABLE"):
                name = sql.rsplit(".", 1)[-1].strip("[]")
                if name == "tabc_parent" and "tabc_child" in state["tables"]:
                    raise Exception("FK constraint violation")   # parent drops only after child
                state["tables"].remove(name)
        def fetchall():
            if executed and executed[-1].startswith("SELECT t.name"):
                return [(t,) for t in state["tables"]]
            return [(1,)]
        cur = types.SimpleNamespace(execute=execute, fetchall=fetchall, close=lambda: None)
        return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)

    dsn = {**_dsn("striim"), "source_user": "qasource", "source_password": "striim",
           "source_schema": "qasource"}
    MssqlAdmin(dsn, connect=connect).drop_test_tables("tabc_")
    assert state["tables"] == ["other"]                       # prefix filter respected
    assert "DROP TABLE [qasource].[tabc_parent]" in executed  # retried after the child
