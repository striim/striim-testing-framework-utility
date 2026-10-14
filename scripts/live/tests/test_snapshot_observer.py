from types import SimpleNamespace

import pytest

from livetest import snapshot_observer as observer, canon
from livetest.assertions import AssertionFailed


def spec():
    return {"db": "postgres-target", "tables": {
        "a": {"target": "${PG_TARGET_SCHEMA}.${TID}a", "columns": {"id": "integer"}},
        "b": {"target": "${PG_TARGET_SCHEMA}.${TID}b", "columns": {"id": "integer"}}},
        "prefixes": [{"a": "empty.csv", "b": "empty.csv"}, {"a": "full.csv", "b": "full.csv"}],
        "interval": "10ms"}


def make_observer(rows):
    data = {"empty.csv": b"id\n", "full.csv": b"id\n1\n"}
    inputs = SimpleNamespace(golden=lambda p: data[p.name])
    return observer.Observer(spec(), admins={}, tokens={}, case_dir=".", inputs=inputs,
                             read=lambda *a: rows)


def test_whole_snapshot_matches_a_prefix():
    o = make_observer({"a": [{"id": 1}], "b": [{"id": 1}]})
    o.sample()
    assert o.history[0]["prefix"] == 1


def test_partial_transaction_is_rejected():
    o = make_observer({"a": [{"id": 1}], "b": []})
    with pytest.raises(AssertionFailed, match="snapshot-not-committed-prefix"):
        o.sample()


def test_duplicate_is_rejected():
    o = make_observer({"a": [{"id": 1}, {"id": 1}], "b": [{"id": 1}]})
    with pytest.raises(AssertionFailed, match="snapshot-not-committed-prefix"):
        o.sample()


@pytest.mark.parametrize("change", [lambda s: s.update(db="mysql-target"), lambda s: s.update(unknown=1),
    lambda s: s.update(interval=0), lambda s: s["prefixes"][0].pop("b"),
    lambda s: s["prefixes"][0].update(a="../escape.csv")])
def test_bad_snapshot_specs_are_refused(change):
    s = spec(); change(s)
    with pytest.raises(ValueError, match="invalid-snapshot-spec"):
        observer.normalize(s, "p", lifecycle=True)


def test_pg_one_connection_one_repeatable_read(monkeypatch):
    from livetest import lifecycle
    calls = []
    connection = object()
    class Probe:
        def _call(self, sql, params, remaining, fetch, read):
            return read(connection, lambda: False)
    monkeypatch.setattr(lifecycle, "PgProbe", lambda admin: Probe())
    def read(conn, table, order, rows, bytes_, cancelled, **kw):
        calls.append((conn, kw["start_snapshot"]))
        return ["id"], [(1,)]
    monkeypatch.setattr(lifecycle, "exact_read", read)
    result = observer.read_snapshot(spec(), {"postgres-target": {"admin": object()}},
                                    {"PG_TARGET_SCHEMA": "qatarget", "TID": "case_"}, 2)
    assert result == {"a": [{"id": 1}], "b": [{"id": 1}]}
    assert calls == [(connection, True), (connection, False)]


def test_two_table_snapshot_does_not_reconfigure_an_open_psycopg_transaction(monkeypatch):
    """Model psycopg2's autocommit setter and transaction state, run real exact_read."""
    from livetest import lifecycle
    calls = []
    class Cursor:
        description = [("row_bytes",), ("id",)]
        def __init__(self, conn): self.conn, self.fetched = conn, False
        def execute(self, sql, params=None):
            self.conn.active = True
            calls.append(sql)
        def fetchone(self): return (1, 2, 1)
        def fetchmany(self, size):
            if self.fetched: return []
            self.fetched = True
            return [(1, 1)]
        def close(self): pass
    class Connection:
        active = False
        @property
        def autocommit(self): return False
        @autocommit.setter
        def autocommit(self, value):
            if self.active:
                raise RuntimeError("set_session cannot be used inside a transaction")
            calls.append("configure session")
        def cursor(self, name=None): return Cursor(self)
    conn = Connection()
    class Probe:
        def _call(self, sql, params, remaining, fetch, read):
            return read(conn, lambda: False)
    monkeypatch.setattr(lifecycle, "PgProbe", lambda admin: Probe())
    result = observer.read_snapshot(spec(), {"postgres-target": {"admin": object()}},
                                   {"PG_TARGET_SCHEMA": "qatarget", "TID": "case_"}, 2)
    assert result == {"a": [{"id": 1}], "b": [{"id": 1}]}
    assert calls.count("configure session") == 1
    assert calls.count("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY") == 1


def test_spanner_one_multi_use_snapshot(monkeypatch):
    from livetest import exactdata
    calls = []
    class Snapshot:
        def __enter__(self): return self
        def __exit__(self, *a): calls.append("closed")
    snapshot = Snapshot()
    class Database:
        def snapshot(self, **kw):
            assert kw == {"multi_use": True}
            calls.append("opened"); return snapshot
    admin = SimpleNamespace(_db=lambda: Database())
    def read(*a, **kw):
        assert kw["snapshot"] is snapshot
        calls.append("read"); return [{"id": 1}]
    monkeypatch.setattr(exactdata, "read_spanner", read)
    s = spec(); s["db"] = "spanner-google"
    observer.read_snapshot(s, {"spanner-google": {"admin": admin}},
                           {"PG_TARGET_SCHEMA": "qatarget", "TID": "case_"}, 2)
    assert calls == ["opened", "read", "read", "closed"]


def test_abandon_retains_history_when_lifecycle_failed_before_finish():
    from datetime import datetime
    o = make_observer({'a': [], 'b': []})
    o.sample()
    record = o.abandon('lifecycle completion deadline')
    assert record['status'] == 'failed' and 'observer-incomplete' in record['detail']
    assert record['actual']['rows'] == o.history
    assert datetime.fromisoformat(o.history[0]['observedUtc']).utcoffset().total_seconds() == 0
    assert o.abandon('second cleanup') is None


def test_finished_observer_is_not_abandoned():
    o = make_observer({'a':[{'id':1}], 'b':[{'id':1}]})
    assert o.finish()[0]['status'] == 'passed'
    assert o.abandon('teardown') is None


def test_worker_error_reports_immediately_and_abandon_keeps_it():
    import threading
    gate, reported = threading.Event(), threading.Event()
    calls, messages = [], []
    o = make_observer({'a': [], 'b': []})
    def read(*args):
        calls.append(1)
        if len(calls) > 2:
            assert gate.wait(2)
            return {'a':[{'id':1}], 'b':[]}
        return {'a':[], 'b':[]}
    o.read = read
    o.report = lambda msg: (messages.append(msg), reported.set())
    o.start(); gate.set()
    assert reported.wait(2)
    record = o.abandon('completion failed')
    assert 'snapshot-not-committed-prefix' in record['detail']
    assert record['actual']['rows'][-1]['prefix'] is None
    assert any('snapshot-not-committed-prefix' in m for m in messages)


def test_plugin_retains_observer_records_on_an_earlier_case_failure():
    from livetest.plugin import _abandon_snapshot
    records = []; o = make_observer({'a': [], 'b': []}); o.sample()
    with pytest.raises(RuntimeError, match='completion failed'):
        try:
            raise RuntimeError('completion failed')
        finally:
            _abandon_snapshot(o, records, 'completion failed')
    assert len(records) == 1 and records[0]['status'] == 'failed'
    assert records[0]['actual']['rows'][0]['prefix'] == 0


def test_partial_snapshot_records_keys_for_each_table():
    o = make_observer({'a': [{'id': 1}], 'b': []})
    with pytest.raises(AssertionFailed):
        o.sample()
    record = o.abandon('completion failed')
    tables = record['actual']['rows'][-1]['tables']
    assert tables['a'] == {'keyColumns': ['id'], 'keys': [['1']], 'rows': 1,
                           'keysTruncated': False, 'valuesTruncated': False}
    assert tables['b']['keys'] == [] and tables['b']['rows'] == 0


@pytest.mark.parametrize('keys', [[], ['missing'], ['id', 'id'], 'id', [1]])
def test_invalid_diagnostic_keys_are_refused(keys):
    s = spec(); s['tables']['a']['keys'] = keys
    with pytest.raises(ValueError, match='invalid-snapshot-spec'):
        observer.normalize(s, 'p', lifecycle=True)


def test_diagnostic_projection_does_not_weaken_prefix_comparison():
    s = spec()
    for table in s['tables'].values():
        table['columns']['msg'] = 'text'
        table['keys'] = ['id']
    observer.normalize(s, 'p', lifecycle=True)
    data = {'empty.csv': b'id,msg\n', 'full.csv': b'id,msg\n1,correct\n'}
    o = observer.Observer(s, admins={}, tokens={}, case_dir='.',
        inputs=SimpleNamespace(golden=lambda p: data[p.name]),
        read=lambda *a: {'a': [{'id': 1, 'msg': 'wrong'}],
                         'b': [{'id': 1, 'msg': 'correct'}]})
    with pytest.raises(AssertionFailed, match='snapshot-not-committed-prefix'):
        o.sample()
    assert o.history[-1]['tables']['a']['keys'] == [['1']]


def test_diagnostic_keys_have_explicit_bounds():
    s = spec(); s['tables']['a']['columns']['id'] = 'text'
    data = {'empty.csv': b'id\n', 'full.csv': b'id\n1\n'}
    o = observer.Observer(s, admins={}, tokens={}, case_dir='.',
        inputs=SimpleNamespace(golden=lambda p: data[p.name]),
        read=lambda *a: {'a': [{'id': 'x' * 300}] * 51, 'b': []})
    with pytest.raises(AssertionFailed):
        o.sample()
    table = o.history[-1]['tables']['a']
    assert table['rows'] == 51 and len(table['keys']) == 50
    assert table['keysTruncated'] and table['valuesTruncated']
    assert all(values == ['x' * 256] for values in table['keys'])


@pytest.mark.parametrize('case', ['samples/live/12-snapshot-observer',
    'scripts/live/regression/framework/framework-snapshot-observer'])
def test_snapshot_sample_preserves_source_transactions(case):
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    tql = (root / case / 'app.tql').read_text()
    assert 'PreserveSourceTransactionBoundary: true' in tql
    assert 'FilterTransactionBoundaries: false' in tql


def test_a_declared_prefix_cannot_regress_after_a_newer_commit():
    full = {'a':[{'id':1}], 'b':[{'id':1}]}
    empty = {'a':[], 'b':[]}
    snapshots = iter([empty, full, full, empty, full])
    o = make_observer(empty)
    o.read = lambda *a: next(snapshots)
    o.sample(); o.sample(); o.sample()
    with pytest.raises(AssertionFailed, match='snapshot-prefix-regressed') as err:
        o.sample()
    assert [e['prefix'] for e in o.history] == [0,1,1,0]
    assert err.value.records[0]['actual']['rows'] == o.history
