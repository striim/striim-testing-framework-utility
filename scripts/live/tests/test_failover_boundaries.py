"""Failover retry boundaries and supported setup declarations."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from livetest import node_fault
from livetest.assertions import AssertionFailed
from livetest.manifest import _normalize_recover, ManifestError, load_manifest

ROOT = Path(__file__).resolve().parents[3]


class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds


def test_placement_retries_a_transient_read_and_keeps_the_last_error():
    clock = Clock(); calls = []
    def read(flow, remaining):
        calls.append(remaining)
        if len(calls) == 1: raise ValueError('server temporarily missing')
        return {'server_primary'}
    result = node_fault.assert_placement([{'flow': '${NS}.flow', 'node': 'server_primary'}],
        client=None, ctx=None, tokens={'NS': 'N'}, timeout=1, read=read,
        now=clock.now, sleep=clock.sleep)
    assert len(calls) == 2 and result[0]['status'] == 'passed'
    assert 'server temporarily missing' in result[0]['detail']


def test_placement_read_errors_retry_until_deadline_and_are_retained():
    clock = Clock(); calls = []
    def read(flow, remaining):
        calls.append(remaining); raise ValueError('server still unavailable')
    with pytest.raises(AssertionFailed, match='placement-read-error') as error:
        node_fault.assert_placement([{'flow': '${NS}.flow', 'node': 'server_primary'}],
            client=None, ctx=None, tokens={'NS': 'N'}, timeout=1, read=read,
            now=clock.now, sleep=clock.sleep)
    assert len(calls) > 1 and clock.t == pytest.approx(1)
    assert 'server still unavailable' in error.value.records[-1]['detail']


def test_hosting_fault_examples_use_the_owned_secondary_worker_group():
    text = (ROOT / 'docs/TEST-YAML.md').read_text()
    examples = [yaml.safe_load(block) for block in re.findall(r'```yaml\n(.*?)```', text, re.S)]
    examples = [e for e in examples if isinstance(e, dict) and isinstance(e.get('recover'), dict)
                and isinstance(e['recover'].get('node'), dict)]
    assert examples
    for example in examples:
        group = example['deployment_groups'][0]
        assert group['servers'] == ['${STRIIM_SECONDARY_SERVER}']
        assert group['add_after_deploy'] == ['${STRIIM_PRIMARY_SERVER}']


def test_repeated_hosting_fault_is_refused_before_setup():
    with pytest.raises(ManifestError, match='invalid-node-fault'):
        _normalize_recover({'mode': 'kill', 'times': 2, 'node': {'hosting': 'N.flow'}}, 'test.yaml')


def test_default_xfail_does_not_hide_placement_but_explicit_tier_is_allowed(tmp_path):
    p = tmp_path / 'test.yaml'; (tmp_path / 'app.tql').write_text('CREATE NAMESPACE ${NS};')
    text = "name: placement\npurpose: Prove placement.\ntql: app.tql\nassert: {placement: [{flow: 'N.flow', node: server_primary}]}\n"
    p.write_text(text + 'xfail: known issue\n')
    assert 'placement' not in load_manifest(p).xfail['tiers']
    p.write_text(text + 'xfail: {reason: known issue, tiers: [placement]}\n')
    assert 'placement' in load_manifest(p).xfail['tiers']


def test_setup_inventory_accepts_a_scheme_less_origin(monkeypatch):
    monkeypatch.setattr(node_fault.stack, 'striim_container', lambda: 'primary')
    monkeypatch.setattr(node_fault.stack, 'app_nodes', lambda: ['primary', 'secondary'])
    monkeypatch.setattr(node_fault.stack, 'agent_container', lambda: 'agent')
    c = SimpleNamespace(list_deployment_groups=lambda **kw: [{'output': [{'default': {'actualServers': [
        {'name': 'server_' + r} for r in ('primary', 'secondary', 'agent')]}}]}])
    def inspect(argv):
        role = argv[-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps([{'Id': 'id_' + role,
            'State': {'Running': True}, 'Config': {'Env': ['JAVA_SYSTEM_PROPERTIES=-Dstriim.node.server.name=server_' + role]},
            'NetworkSettings': {'Ports': {'9080/tcp': [{'HostPort': '19082'}]}}}]))
    nodes = node_fault.inventory(c, SimpleNamespace(mode='docker', url='host:19080'), run=inspect)
    assert nodes['secondary']['url'] == 'http://host:19082'


@pytest.mark.parametrize('succeeded', [True, False])
def test_initial_placement_retries_and_records_owners_and_errors(succeeded):
    clock = Clock(); records = []; calls = []
    def read(flow, remaining):
        calls.append(remaining)
        if len(calls) == 1: raise ValueError('status not ready')
        return {'server_secondary'} if succeeded else set()
    args = dict(client=None, flow='N.flow', servers={'server_secondary'}, timeout=1,
                record=records.append, read=read, now=clock.now, sleep=clock.sleep)
    if succeeded: node_fault.initial_placement(**args)
    else:
        with pytest.raises(node_fault.RecoveryError, match='deployment-group-initial-placement'):
            node_fault.initial_placement(**args)
    assert len(calls) > 1
    row = records[-1]['actual']['rows'][0]
    assert row['phase'] == 'initial-placement' and 'status not ready' in row['lastReadError']
    assert records[-1]['status'] == ('passed' if succeeded else 'failed')


def test_explicit_create_rejection_does_not_acquire_a_racing_group(tmp_path):
    from tests.test_owned_placement import ledger, Client
    from livetest.striim import StriimError
    l = ledger(tmp_path); c = Client(); name = l.ident.tid + 'workers'
    def rejected(text):
        c.groups[name] = {'server_secondary'}
        raise StriimError('group already exists')
    c.deploy_tql = rejected
    with pytest.raises(StriimError): l.acquire_deployment_group(c, name, ['server_secondary'])
    assert l.entries[0]['state'] == 'intended'


def test_cluster_membership_poll_has_a_per_read_cap(monkeypatch):
    from livetest import striim_provision as sp
    budgets = []
    monkeypatch.setattr(sp, 'parse_deployment_groups', lambda r: SimpleNamespace(has_cluster=True, has_agent=True))
    client = SimpleNamespace(list_deployment_groups=lambda timeout: budgets.append(timeout))
    sp.wait_cluster_ready(client, timeout=300)
    assert 0 < budgets[0] <= 120


def test_setup_inventory_is_refreshed_after_restore(monkeypatch):
    from tests.test_node_faults import nodes, Client
    before = nodes(); refreshed = nodes(); refreshed['secondary']['server'] = 'server_rejoined'
    monkeypatch.setattr(node_fault, 'inventory', lambda *args, **kw: refreshed)
    monkeypatch.setattr(node_fault, '_reset_modules', lambda: None)
    target = node_fault.Target(Client(), SimpleNamespace(mode='docker'), 'secondary', app='N.app',
        nodes=before, refresh=True, wait_cluster=lambda *args: None)
    target.restore()
    assert target.nodes['secondary']['server'] == 'server_rejoined'
