"""Commit diagnostics observe the source without closing overlapping seed sessions."""
from types import SimpleNamespace
import re
import pytest
from livetest import transactions

OBSERVE = {'tables': [{'table': '${PG_SOURCE_SCHEMA}.${TID}src', 'key': 'id'}],
           'slot': '${PG_SLOT}'}
TOKENS = {'PG_SOURCE_SCHEMA': 'qasource', 'TID': 'case_', 'PG_SLOT': 'case_slot'}

class Source:
    def __init__(self):
        self.visible, self.committed, self.connections, self.queries = set(), set(), [], []
    def connect(self, admin, timeout):
        conn = Connection(self, len(self.connections) + 100)
        self.connections.append(conn)
        return conn
    def probe(self, admin): return self
    def query(self, sql, params=None, remaining=1):
        self.queries.append((sql, params, remaining))
        assert remaining > 0
        if 'txid_status' in sql:
            return [('committed' if params[0] in self.committed else 'in progress',)]
        if 'pg_replication_slots' in sql:
            return [('0/20', '0/10')]
        return [(str(n), False) for n in sorted(self.visible)]

class Connection:
    def __init__(self, source, txid):
        self.source, self.txid, self.pending, self.closed = source, txid, set(), False
    def cursor(self): return Cursor(self)
    def commit(self):
        self.source.visible.update(self.pending)
        self.source.committed.add(self.txid)
    def rollback(self): self.pending.clear()
    def close(self): self.closed = True

class Cursor:
    def __init__(self, conn): self.conn = conn
    def execute(self, sql, params=None):
        if sql.startswith('INSERT'):
            self.conn.pending.add(int(re.search(r'VALUES\s*\((\d+)\)', sql).group(1)))
    def fetchone(self): return (self.conn.txid,)
    def close(self): pass

def pool(source, records, owned=lambda *args: True):
    return transactions.Pool({'postgres-source': {'admin': object()}}, TOKENS,
        source_sql=lambda f: 'INSERT INTO case_src VALUES (' + ('1' if f == 'a.sql' else '2') + ')',
        connect=source.connect, probe=source.probe, owned=owned, trace=records.append)

def steps():
    return [{'session': 'a', 'op': 'begin', 'db': 'postgres-source', 'observe': OBSERVE, 'when': 'post_start'},
            {'session': 'b', 'op': 'begin', 'db': 'postgres-source', 'observe': OBSERVE, 'when': 'post_start'},
            {'session': 'a', 'op': 'sql', 'file': 'a.sql', 'when': 'post_start'},
            {'session': 'b', 'op': 'sql', 'file': 'b.sql', 'when': 'post_start'},
            {'session': 'b', 'op': 'commit', 'when': 'post_start'},
            {'session': 'a', 'op': 'commit', 'when': 'post_recover'}]

def test_observed_begin_schedule_loads():
    result = transactions.normalize(steps(), 'case', recover=True, lifecycle=True)
    assert result[0]['observe'] == OBSERVE

@pytest.mark.parametrize('observe', [{}, {'tables': []}, {'tables': [{'table': 'foreign', 'key': 'id'}]},
    {'tables': [{'table': '${TID}src', 'key': 'id;bad'}]}, {**OBSERVE, 'unknown': True}])
def test_observe_refuses_unknown_or_unbounded_references(observe):
    schedule = steps(); schedule[0] = {**schedule[0], 'observe': observe}
    with pytest.raises(ValueError, match='invalid-transaction'):
        transactions.normalize(schedule, 'case', recover=True, lifecycle=True)

def test_commit_proof_and_slot_reads_preserve_held_session():
    source, records = Source(), []
    p = pool(source, records)
    p.run_phase(steps(), 'post_start')
    assert not source.connections[0].closed
    p.observe_recovery('pre_interrupt')
    p.observe_recovery('post_restore')
    assert not source.connections[0].closed and source.visible == {2}
    p.run_phase(steps(), 'post_recover')
    commits = [r['actual']['rows'][0] for r in records if r['spec'].get('op') == 'commit']
    assert [e['txid'] for e in commits] == [101,100]
    assert all(e['commitSucceeded'] and e['commitStatus'] == 'committed' for e in commits)
    assert [e['source']['tables'][0]['keys'] for e in commits] == [['2'],['1','2']]
    checkpoints = [r['actual']['rows'][0] for r in records if r['spec'].get('phase') in ('pre_interrupt','post_restore')]
    assert [e['phase'] for e in checkpoints] == ['pre_interrupt','post_restore']
    assert all(e['slot']['confirmedFlushLsn'] == '0/20' and e['slot']['restartLsn'] == '0/10' for e in checkpoints)
    assert all(e['observedUtc'].endswith('+00:00') for e in checkpoints)
    assert source.connections[0].closed

def test_unowned_observe_refused_before_connection_or_query():
    source = Source(); p = pool(source, [], owned=lambda *args: False)
    with pytest.raises(ValueError, match='transaction-observe-not-owned'):
        p.run_phase(steps(), 'post_start')
    assert not source.connections and not source.queries

def test_failed_readback_distinguishes_committed_from_failed_evidence():
    source, records = Source(), []
    def unreadable(sql, params=None, remaining=1): raise RuntimeError('readback unavailable')
    source.query = unreadable
    p = pool(source, records)
    with pytest.raises(Exception, match='readback unavailable'):
        p.run_phase(steps(), 'post_start')
    event = records[-1]['actual']['rows'][0]
    assert event['commitSucceeded'] is True and event['outcome'] == 'failed'
    assert records[-1]['status'] == 'failed' and source.visible == {2}
    assert all(c.closed for c in source.connections)


def test_reused_session_without_observe_does_not_inherit_commit_diagnostics():
    source, records = Source(), []
    p = pool(source, records)
    schedule = [steps()[0], {'session':'a','op':'commit','when':'post_start'},
        {'session':'a','op':'begin','db':'postgres-source','when':'post_start'},
        {'session':'a','op':'commit','when':'post_start'}]
    p.run_phase(schedule,'post_start')
    commits = [r['actual']['rows'][0] for r in records if r['spec'].get('op') == 'commit']
    assert 'txid' in commits[0] and 'txid' not in commits[1]
