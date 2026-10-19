"""Fixture SQL has bounded connections/statements and only observes sibling blockers."""
import threading

import pytest

from livetest import pgclient
from livetest.pgclient import PgAdmin
from livetest.plugin import _run_admin_sql


DSN = {"host": "db", "port": 5432, "dbname": "tests",
       "admin_user": "postgres", "admin_password": "pw",
       "source_user": "qasource", "source_password": "pw"}
SLOT_SQL = "SELECT * FROM pg_create_logical_replication_slot('slt_probe','wal2json')"


class SqlTimeout(Exception):
    pgcode = "57014"


class Connection:
    def __init__(self, calls, *, blocking=False, captured=None, diagnostic=False):
        self.calls, self.blocking, self.captured = calls, blocking, captured
        self.diagnostic, self.closed = diagnostic, False
        self.autocommit = False

    def get_backend_pid(self):
        return 100 if self.blocking else None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "pg_blocking_pids" in sql:
            self.captured.set()
        if self.blocking and sql == SLOT_SQL:
            assert self.captured.wait(5), "no passive blocker observation while SQL was waiting"
            raise SqlTimeout("canceling statement due to statement timeout")

    def fetchall(self):
        return [(9001, "$STRIIM$ - SLT_sibling.Target", 123.5, "idle in transaction")]

    def close(self):
        self.closed = True


def admin_spy():
    connections, calls, options = [], [], []

    def connect(**kw):
        options.append(kw)
        conn = Connection(calls)
        connections.append(conn)
        return conn

    return PgAdmin(dict(DSN), connect=connect), connections, calls, options


def test_default_fixture_connection_has_server_and_connect_deadlines():
    admin, connections, calls, options = admin_spy()
    admin.run_sql("CREATE TABLE t (id integer)")
    assert options[0]["connect_timeout"] == 5
    assert "lock_timeout=15000" in options[0]["options"]
    assert "statement_timeout=60000" in options[0]["options"]
    assert "search_path" in calls[0][0]
    assert all(conn.closed for conn in connections)


def test_seed_uses_its_own_longer_statement_budget_and_run_tag():
    admin, _, _, options = admin_spy()
    admin.run_sql("INSERT INTO t VALUES (1)", phase="seed", timeout=1800, run_id="run-current")
    assert "statement_timeout=1800000" in options[0]["options"]
    assert "lock_timeout=15000" in options[0]["options"]
    assert "run-current" in options[0]["application_name"]
    assert "seed" in options[0]["application_name"]


def test_slot_timeout_reports_actual_blocker_without_terminating_it(monkeypatch):
    captured = threading.Event()
    calls, connections, options = [], [], []

    def connect(**kw):
        options.append(kw)
        conn = Connection(calls, blocking=kw["user"] != "postgres", captured=captured,
                          diagnostic=kw["user"] == "postgres")
        connections.append(conn)
        return conn

    admin = PgAdmin(dict(DSN), connect=connect)
    monkeypatch.setattr(admin, "_exec_ddl", lambda *a, **k: pytest.fail("terminating DDL watcher used"))
    monkeypatch.setattr(admin, "_terminate_idle_blockers_of", lambda *a: pytest.fail("blocker killed"))
    with pytest.raises(RuntimeError, match="postgres-fixture-timeout") as failed:
        admin.run_sql(SLOT_SQL, phase="ddl", run_id="run-current", owners={"SLT_sibling": "run-sibling"})
    text = str(failed.value)
    for value in ("slot-setup", "postgres-source", "9001", "SLT_sibling.Target", "123.5", "run-sibling"):
        assert value in text
    assert not admin.terminated
    assert not any("pg_terminate_backend" in sql or "pg_cancel_backend" in sql for sql, _ in calls)
    assert all(conn.closed for conn in connections)
    diagnostic = [kw for kw in options if kw["user"] == "postgres"]
    assert diagnostic and all(kw["connect_timeout"] == 2 for kw in diagnostic)
    assert all("statement_timeout=1000" in kw["options"] for kw in diagnostic)


def test_fixture_connection_failure_retains_cold_start_retry(monkeypatch):
    attempts = []

    def fail(**kw):
        attempts.append(kw)
        raise OSError("connection unavailable")

    monkeypatch.setattr(pgclient.time, "sleep", lambda seconds: None)
    admin = PgAdmin(dict(DSN), connect=fail)
    with pytest.raises(RuntimeError, match="postgres-fixture-connect-failed"):
        admin.run_sql("SELECT 1")
    assert len(attempts) == 30
    assert all(attempt["connect_timeout"] == 5 for attempt in attempts)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), True])
def test_invalid_fixture_deadline_is_rejected_before_connect(timeout):
    admin, _, _, options = admin_spy()
    with pytest.raises(ValueError, match="invalid-postgres-fixture-budget"):
        admin.run_sql("SELECT 1", phase="seed", timeout=timeout)
    assert not options


def test_dispatch_passes_seed_phase_and_budget_only_to_postgres():
    admin, _, _, options = admin_spy()
    _run_admin_sql({"admin": admin, "schema": None}, "INSERT INTO t VALUES (1)",
                   phase="seed", timeout=1200, run_id="run-current", owners={})
    assert "statement_timeout=1200000" in options[0]["options"]


def test_other_database_dispatch_keeps_the_existing_call_shape():
    class OtherAdmin:
        def run_sql(self, sql):
            self.sql = sql

    admin = OtherAdmin()
    _run_admin_sql({"admin": admin}, "SELECT 1", phase="seed", timeout=1200,
                   run_id="run-current", owners={})
    assert admin.sql == "SELECT 1"


def test_blocker_run_lookup_uses_real_ownership_ledgers(tmp_path):
    from livetest import ownership, plugin, runident

    current = runident.derive("current", {"SLT_RUN_EPOCH": "run-current"})
    sibling = runident.derive("sibling", {"SLT_RUN_EPOCH": "run-sibling"})
    ledgers = [ownership.Ledger.open(ident, state_dir=tmp_path, env={})
               for ident in (current, sibling)]
    for ledger in ledgers:
        ledger.add("namespace", ledger.ident.ns, state="confirmed")
    owners = plugin._pg_fixture_owners(ledgers[0])
    assert owners[current.ns] == "run-current"
    assert owners[sibling.ns] == "run-sibling"
    assert "unrecorded" not in owners
