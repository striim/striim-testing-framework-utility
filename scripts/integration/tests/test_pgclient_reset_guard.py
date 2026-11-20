"""The guard on `PgAdmin.reset_schemas()` (`inttest/pgclient.py` `_assert_reset_is_safe`).

`reset_schemas()` runs `DROP SCHEMA ... CASCADE` on qasource/qatarget as the admin. An
`INT_PG_DB` / `INT_PG_HOST` override can point it at any Postgres, so it refuses a dbname other
than the harness's own sandbox unless `INT_PG_ALLOW_DESTRUCTIVE_RESET=1` confirms the database
is safe to wipe. `test_pgclient.py` is `docker`-marked as a whole, so these hermetic tests live
here: `PgAdmin` takes a `connect` callable and nothing reaches a database.
"""
from __future__ import annotations

import pytest

from inttest import pgclient
from inttest.pgclient import PgAdmin, UnsafeResetError

_ENV = "INT_PG_ALLOW_DESTRUCTIVE_RESET"


class _Conn:
    def __init__(self, sent: list):
        self._sent = sent
        self.autocommit = False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self._sent.append(sql)

    def close(self):
        pass


def _admin(dbname, sent: list, opened: list):
    dsn = {"host": "h", "port": "5432", "dbname": dbname, "admin_user": "postgres", "admin_password": "x",
           "source_user": "qasource", "source_password": "x", "target_user": "qatarget", "target_password": "x"}

    def connect(**kw):
        opened.append(kw)
        return _Conn(sent)
    return PgAdmin(dsn, connect=connect)


_DROPS = ['DROP SCHEMA IF EXISTS "qasource" CASCADE', 'DROP SCHEMA IF EXISTS "qatarget" CASCADE']


@pytest.mark.parametrize("dbname", ["postgres", "shared", "INTDB"])
def test_reset_schemas_refuses_a_database_that_is_not_the_sandbox(monkeypatch, dbname):
    monkeypatch.delenv(_ENV, raising=False)
    sent, opened = [], []
    with pytest.raises(UnsafeResetError, match=rf"refusing reset_schemas\(\): dbname {dbname!r} .*{_ENV}=1"):
        _admin(dbname, sent, opened).reset_schemas()
    assert opened == [] and sent == [], "the refusal comes before any connection"


def test_reset_schemas_runs_on_the_sandbox_database(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    sent, opened = [], []
    _admin("intdb", sent, opened).reset_schemas()
    assert [s for s in sent if s.startswith("DROP SCHEMA")] == _DROPS
    assert [kw.get("dbname") for kw in opened] == ["intdb"]


@pytest.mark.parametrize("value, allowed", [("1", True), ("0", False), ("true", None), ("yes", None),
                                            (" 1", None), ("", False)])
def test_the_destructive_reset_opt_in_is_exactly_one(monkeypatch, value, allowed):
    """None: not a switch value at all, refused as a settings mistake (paths.flag)."""
    monkeypatch.setenv(_ENV, value)
    sent, opened = [], []
    admin = _admin("shared", sent, opened)
    if allowed:
        admin.reset_schemas()
        assert [s for s in sent if s.startswith("DROP SCHEMA")] == _DROPS
    else:
        with pytest.raises(UnsafeResetError if allowed is False else ValueError, match=None if allowed is False else _ENV):
            admin.reset_schemas()
        assert opened == [] and sent == []


def test_the_guard_refuses_a_dsn_without_a_dbname(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    with pytest.raises(UnsafeResetError, match="dbname None"):
        pgclient._assert_reset_is_safe({})
