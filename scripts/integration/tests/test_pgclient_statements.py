"""The statements `PgAdmin` sends for setup and reset, against a recording connection.

`test_pgclient.py` runs these against a real Postgres and is `docker`-marked as a whole, so the
release gate (`-m "not docker"`) never ran them. `PgAdmin` takes a `connect` callable: here it
records every connection and statement and nothing reaches a database.
"""
from __future__ import annotations

import pytest

from inttest import pgclient
from inttest.pgclient import PgAdmin

DSN = {"host": "h", "port": "5432", "dbname": "intdb", "admin_user": "postgres", "admin_password": "adminpw",
       "source_user": "qasource", "source_password": "srcpw", "target_user": "qatarget", "target_password": "tgtpw"}


SETUP = [
    "DO $$ BEGIN CREATE ROLE qasource LOGIN PASSWORD 'srcpw'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    'GRANT CONNECT ON DATABASE "intdb" TO qasource',
    'CREATE SCHEMA IF NOT EXISTS "qasource" AUTHORIZATION qasource',
    "DO $$ BEGIN CREATE ROLE qatarget LOGIN PASSWORD 'tgtpw'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    'GRANT CONNECT ON DATABASE "intdb" TO qatarget',
    'CREATE SCHEMA IF NOT EXISTS "qatarget" AUTHORIZATION qatarget',
]


class _Conn:
    def __init__(self, log: list, rows):
        self.log, self.rows, self.autocommit = log, rows, False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        if callable(self.rows):
            self.description = [("tablename",)]
            self._fetched = self.rows(sql, params)

    def fetchall(self):
        return self._fetched

    def close(self):
        pass


def _admin(log: list, rows=None, dsn=DSN):
    def connect(**kw):
        log.append(("connect", kw["user"], kw["dbname"]))
        return _Conn(log, rows)
    return PgAdmin(dict(dsn), connect=connect)


def _sent(log):
    return [entry[1] for entry in log if entry[0] == "execute"]


def test_ensure_setup_creates_both_roles_grants_connect_and_owned_schemas_as_the_admin():
    log = []
    _admin(log).ensure_setup()
    assert [e for e in log if e[0] == "connect"] == [("connect", "postgres", "intdb")]
    assert _sent(log) == SETUP


def test_ensure_setup_refuses_a_password_it_would_inline_before_connecting():
    log = []
    with pytest.raises(ValueError, match="unsafe source role password"):
        _admin(log, dsn={**DSN, "source_password": "x'; DROP ROLE postgres; --"}).ensure_setup()
    assert log == []


def test_ensure_setup_retries_the_whole_list_on_a_catalog_race_only(monkeypatch):
    monkeypatch.setattr(pgclient.time, "sleep", lambda s: None)
    log, raised = [], []

    class Racing(_Conn):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if sql.startswith("GRANT") and not raised:
                raised.append(1)
                raise RuntimeError('tuple concurrently updated')

    admin = PgAdmin(dict(DSN), connect=lambda **kw: Racing(log, None))
    admin.ensure_setup()
    # the first attempt got as far as the source GRANT; the retry sent the whole list from the top
    assert _sent(log) == SETUP[:2] + SETUP

    log.clear()

    class Refused(_Conn):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            raise RuntimeError('password authentication failed for user "postgres"')

    with pytest.raises(RuntimeError, match="password authentication failed"):
        PgAdmin(dict(DSN), connect=lambda **kw: Refused(log, None)).ensure_setup()
    assert len(_sent(log)) == 1                                                  # not retried


def test_reset_schemas_drops_and_recreates_each_schema_as_the_admin():
    log = []
    _admin(log).reset_schemas()
    assert [e for e in log if e[0] == "connect"] == [("connect", "postgres", "intdb")]
    assert _sent(log) == ['DROP SCHEMA IF EXISTS "qasource" CASCADE', 'CREATE SCHEMA "qasource" AUTHORIZATION qasource',
                          'DROP SCHEMA IF EXISTS "qatarget" CASCADE', 'CREATE SCHEMA "qatarget" AUTHORIZATION qatarget']


def test_reset_test_objects_drops_only_the_tables_carrying_the_tid():
    log = []
    tables = {"qasource": ["t3f9a2b1c4_src"], "qatarget": ["t3f9a2b1c4_tgt", "t3f9a2b1c4_tgt2"]}
    _admin(log, rows=lambda sql, params: [(t,) for t in tables[params[0]]] if params else []).reset_test_objects(
        "t3f9a2b1c4_")
    sent = [e for e in log if e[0] == "execute"]
    lookups = [(sql, params) for _op, sql, params in sent if sql.startswith("SELECT")]
    assert lookups == [
        ("SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s ESCAPE '\\'",
         ("qasource", "t3f9a2b1c4\\_%")),
        ("SELECT tablename FROM pg_tables WHERE schemaname = %s AND tablename LIKE %s ESCAPE '\\'",
         ("qatarget", "t3f9a2b1c4\\_%"))]
    assert [sql for _op, sql, _p in sent if sql.startswith("DROP")] == [
        'DROP TABLE IF EXISTS "qasource"."t3f9a2b1c4_src" CASCADE',
        'DROP TABLE IF EXISTS "qatarget"."t3f9a2b1c4_tgt" CASCADE',
        'DROP TABLE IF EXISTS "qatarget"."t3f9a2b1c4_tgt2" CASCADE']
    assert {e[1] for e in log if e[0] == "connect"} == {"postgres"}


def test_reset_test_objects_sends_no_drop_when_nothing_carries_the_tid():
    log = []
    _admin(log, rows=lambda sql, params: []).reset_test_objects("t3f9a2b1c4_")
    assert not [s for s in _sent(log) if not s.startswith("SELECT")]


def test_reset_test_objects_refuses_an_empty_tid_before_connecting():
    log = []
    with pytest.raises(ValueError, match="tid must be non-empty"):
        _admin(log).reset_test_objects("")
    assert log == []
