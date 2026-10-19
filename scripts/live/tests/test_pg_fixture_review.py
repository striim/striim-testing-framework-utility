"""Regression coverage for passive diagnostics and fixture callers."""
import logging
import threading
import time

from livetest import pgfixture
from livetest.pgclient import PgAdmin
DSN = {"host": "db", "port": 5432, "dbname": "tests",
       "admin_user": "postgres", "admin_password": "pw",
       "source_user": "qasource", "source_password": "pw"}


class Connection:
    def __init__(self, calls):
        self.calls, self.closed = calls, False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def close(self):
        self.closed = True


def test_success_survives_a_still_running_observer(monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    threads, connections = [], []
    real_thread = threading.Thread

    class SlowObserver(real_thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            threads.append(self)

        def join(self, timeout=None):
            return None  # Simulate a diagnostic that outlives the join budget.

    def blockers(*args):
        entered.set()
        assert release.wait(5)
        return []

    class Successful(Connection):
        def get_backend_pid(self):
            return 100

        def execute(self, sql, params=None):
            if sql == 'SELECT work':
                assert entered.wait(4)

    def connect(**kwargs):
        conn = Successful([])
        connections.append(conn)
        return conn

    monkeypatch.setattr(pgfixture.threading, 'Thread', SlowObserver)
    monkeypatch.setattr(pgfixture, '_blockers', blockers)
    monkeypatch.setattr(pgfixture, 'OBSERVE_AFTER_S', 0.01, raising=False)
    try:
        with caplog.at_level(logging.WARNING):
            PgAdmin(dict(DSN), connect=connect).run_sql('SELECT work')
        assert connections[0].closed
        assert 'postgres-fixture-diagnostic-gap' in caplog.text
    finally:
        release.set()
        for thread in threads:
            real_thread.join(thread, 5)


def test_unblocked_statement_opens_no_diagnostic_connections():
    connections = []

    class Unblocked(Connection):
        def get_backend_pid(self):
            return 100

        def execute(self, sql, params=None):
            if sql == 'SELECT work':
                time.sleep(0.4)

    def connect(**kwargs):
        connections.append(kwargs)
        return Unblocked([])

    PgAdmin(dict(DSN), connect=connect).run_sql('SELECT work')
    assert len(connections) == 1


def test_long_statement_has_a_bounded_observation_count(monkeypatch):
    calls = []
    monkeypatch.setattr(pgfixture, 'OBSERVE_AFTER_S', 0.01, raising=False)
    monkeypatch.setattr(pgfixture, 'OBSERVE_INTERVAL_S', 0.01, raising=False)
    monkeypatch.setattr(pgfixture, '_blockers', lambda *a: calls.append(a) or [])

    class LongStatement(Connection):
        def get_backend_pid(self):
            return 100

        def execute(self, sql, params=None):
            if sql == 'SELECT work':
                time.sleep(1)

    PgAdmin(dict(DSN), connect=lambda **kw: LongStatement([])).run_sql('SELECT work')
    assert 1 <= len(calls) <= 3
