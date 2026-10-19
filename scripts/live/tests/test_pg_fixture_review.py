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


def _plugin_function(name, namespace):
    """Execute the real nested caller without provisioning a live app."""
    import ast
    import inspect
    from livetest import plugin
    node = next(n for n in ast.walk(ast.parse(inspect.getsource(plugin)))
                if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<plugin caller>', 'exec'), namespace)
    return namespace[name]


def test_fixture_budget_is_independent_of_assert_timeout(tmp_path):
    from types import SimpleNamespace
    from livetest.manifest import load_manifest
    case = tmp_path / 'test.yaml'
    case.write_text('name: bulk\ntql: app.tql\ntimeout: 7\nfixture_timeout: 1800\nassert: {smoke: true}\n')
    m = load_manifest(case)
    (tmp_path / 'bulk.sql').write_text('SELECT 1')
    calls = []
    env = dict(m=m, admins={}, tokens={}, ledger=None,
               ident=SimpleNamespace(run_id='current'),
               self=SimpleNamespace(_slt_inputs=SimpleNamespace(record=lambda *a, **kw: None)),
               require_service_admin=lambda *a: {}, render=lambda text, tokens: text,
               _run_admin_sql=lambda *a, **kw: calls.append(kw))
    run_files = _plugin_function('_run_files', env)
    run_files([('postgres-source', 'bulk.sql')], phase='ddl')
    run_files([('postgres-source', 'bulk.sql')], phase='seed')
    assert [c['timeout'] for c in calls] == [1800, 1800]
    assert [c['phase'] for c in calls] == ['ddl', 'seed']


def test_default_fixture_budget_does_not_borrow_assert_timeout(tmp_path):
    from livetest.manifest import load_manifest
    case = tmp_path / 'test.yaml'
    case.write_text('name: bulk\ntql: app.tql\ntimeout: 7\nassert: {smoke: true}\n')
    assert load_manifest(case).fixture_timeout is None


def test_concurrent_action_caller_keeps_dml_unbounded(tmp_path):
    from types import SimpleNamespace
    from livetest.plugin import _run_admin_sql
    calls = []
    options = []
    def connect(**kwargs):
        options.append(kwargs)
        return Connection(calls)
    admin = PgAdmin(dict(DSN), connect=connect)
    (tmp_path / 'op.sql').write_text('INSERT INTO t VALUES (1)')
    stop = threading.Event()
    class OneIteration:
        is_set = stop.is_set
        def wait(self, timeout):
            stop.set()
            return True
    caller = _plugin_function('_run_concurrent_op', dict(
        m=SimpleNamespace(source_dir=tmp_path, name='bulk'),
        admins={'postgres-source': {'admin': admin, 'schema': None}},
        tokens={}, ident=SimpleNamespace(run_id='current'), ledger=None,
        _action_stop_flag=OneIteration(), _report=lambda *args: None,
        render=lambda text, tokens: text, _run_admin_sql=_run_admin_sql))
    caller({'db': 'postgres-source', 'file': 'op.sql', 'loop_interval': 0.01})
    assert ('INSERT INTO t VALUES (1)', None) in calls
    assert len(options) == 1
    assert 'options' not in options[0]  # no fixture statement/lock deadlines or observer


import pytest

@pytest.mark.parametrize('value', ['0', '-1', 'true', '.inf', '2147484', '"10min"'])
def test_invalid_manifest_fixture_budget_is_rejected(tmp_path, value):
    from livetest.manifest import load_manifest, ManifestError
    case = tmp_path / 'test.yaml'
    case.write_text(f'name: bulk\ntql: app.tql\nfixture_timeout: {value}\nassert: {{smoke: true}}\n')
    with pytest.raises(ManifestError, match='fixture_timeout'):
        load_manifest(case)


def test_fixture_retries_a_cold_connection_before_sql(monkeypatch):
    attempts, sleeps, calls = [], [], []
    monkeypatch.setattr(pgfixture.time, 'sleep', sleeps.append)
    def connect(**kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise OSError('not ready')
        return Connection(calls)
    PgAdmin(dict(DSN), connect=connect).run_sql('SELECT 1')
    assert len(attempts) == 3
    assert sleeps == [2.0, 2.0]
    assert ('SELECT 1', None) in calls
    assert all(a['connect_timeout'] == 5 for a in attempts)


def test_lock_timeout_is_reported_and_fixture_connection_closed():
    connections = []
    class LockTimeout(Exception):
        pgcode = '55P03'
    class Locked(Connection):
        def execute(self, sql, params=None):
            if sql == 'SELECT locked':
                raise LockTimeout('lock timeout')
    def connect(**kwargs):
        conn = Locked([])
        connections.append(conn)
        return conn
    with pytest.raises(RuntimeError, match='postgres-fixture-timeout: phase=ddl route=postgres-source') as failed:
        PgAdmin(dict(DSN), connect=connect).run_sql('SELECT locked')
    assert failed.value.__cause__.pgcode == '55P03'
    assert 'lock_timeout_ms=15000' in str(failed.value)
    assert all(conn.closed for conn in connections)
