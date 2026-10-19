"""A failover wait retains read failures and placement before restarting its owner."""
from types import SimpleNamespace

import pytest
from livetest import evidence, node_fault, recovery


class Clock:
    def __init__(self): self.t = 0.0
    def sleep(self, seconds): self.t += seconds
    def now(self): return self.t


def target_for(monkeypatch, read, *, timeout=1):
    clock, records, commands = Clock(), [], []
    monkeypatch.setattr(node_fault.time, 'monotonic', clock.now)
    monkeypatch.setattr(node_fault.time, 'sleep', clock.sleep)
    monkeypatch.setattr(node_fault, '_reset_modules', lambda: None)
    class Client:
        def configure_failover(self, *a, **kw): pass
        def await_running(self, *a, **kw): pass
        def current_status(self, *a, **kw): return 'RUNNING'
    nodes = {role: {'role': role, 'server': 'server_' + role, 'id': 'id_' + role,
                    'url': 'endpoint_' + role} for role in ('primary', 'secondary', 'agent')}
    reads = [0]
    def placement(flow, left):
        reads[0] += 1
        if reads[0] == 1: return {'server_primary'}
        return read(clock, reads[0] - 1)
    def command(argv):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout='false')
    target = node_fault.Target(Client(), SimpleNamespace(mode='docker'), {'hosting': 'N.flow'},
        app='N.app', nodes=nodes, placement=placement, run=command,
        record=records.append, timeout=timeout, wait_cluster=lambda *a: None)
    target.interrupt('kill')
    return target, clock, records, commands


def test_success_retains_errors_and_empty_placement_before_the_survivor(monkeypatch):
    def read(clock, index):
        if index == 1: raise ValueError('placement-node-status-unavailable')
        return set() if index == 2 else {'server_secondary'}
    target, clock, records, commands = target_for(monkeypatch, read)
    target.restore()
    wait_record = next(r for r in records if ((r.get('actual') or {}).get('rows') or [{}])[0].get('phase') == 'failover-wait')
    row = wait_record['actual']['rows'][0]
    assert row['phase'] == 'failover-wait' and wait_record['status'] == 'passed'
    assert [r['owners'] for r in row['observations']] == [None, [], ['server_secondary']]
    assert row['observations'][0]['error'] == 'placement-node-status-unavailable'
    assert row['observationCount'] == 3 and row['observationsDropped'] == 0
    assert row['survivor'] == 'server_secondary' and row['interrupted'] is True
    assert row['deadline'] == 1 and clock.t < 1
    assert commands[-1] == ['docker', 'start', 'id_primary']


def test_failed_wait_keeps_every_bounded_error_and_does_not_restart_early(monkeypatch):
    target, clock, records, commands = target_for(monkeypatch,
        lambda clock, index: (_ for _ in ()).throw(ValueError('unreadable placement')))
    with pytest.raises(recovery.RecoveryError, match='fault-node-no-failover'):
        target.restore()
    row = records[-1]['actual']['rows'][0]
    assert records[-1]['status'] == 'failed' and row['phase'] == 'failover-wait'
    assert row['observations'] and all(r['error'] == 'unreadable placement' for r in row['observations'])
    assert row['observationCount'] == len(row['observations'])
    assert clock.t == pytest.approx(1)
    assert not any(a[1] == 'start' for a in commands)
    target.close()
    assert commands[-1] == ['docker', 'start', 'id_primary']


def test_history_and_error_text_have_explicit_bounds_and_redaction(monkeypatch):
    monkeypatch.setattr(node_fault, 'MAX_FAILOVER_OBSERVATIONS', 4, raising=False)
    monkeypatch.setattr(evidence, 'known_secrets', lambda: ('private-test-secret',))
    def read(clock, index):
        raise ValueError('private-test-secret ' + 'x' * 5000)
    target, clock, records, commands = target_for(monkeypatch, read, timeout=2)
    with pytest.raises(recovery.RecoveryError): target.restore()
    row = records[-1]['actual']['rows'][0]
    assert len(row['observations']) == 4
    assert row['observationCount'] > 4 and row['observationsDropped'] == row['observationCount'] - 4
    assert row['observations'][-1]['index'] == row['observationCount']
    assert all(len(r['error']) <= 1024 and r['errorTruncated'] for r in row['observations'])
    assert 'private-test-secret' not in str(records)


def test_placement_returning_after_the_deadline_cannot_pass(monkeypatch):
    def read(clock, index):
        clock.t = 2
        return {'server_secondary'}
    target, clock, records, commands = target_for(monkeypatch, read)
    with pytest.raises(recovery.RecoveryError, match='fault-node-no-failover'): target.restore()
    row = records[-1]['actual']['rows'][0]
    assert records[-1]['status'] == 'failed'
    assert row['observations'][-1]['owners'] == ['server_secondary']
    assert row['observations'][-1]['late'] is True
    assert not any(a[1] == 'start' for a in commands)
