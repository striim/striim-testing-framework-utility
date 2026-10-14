"""Captured 5.4.2 flow MON lacks status; server STATUS supplies runtime state."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from livetest import node_fault, recovery
from livetest.striim import StriimClient

FIXTURE = Path(__file__).parent / 'fixtures/striim/flow-placement-5.4.2.json'


def replies(single=False):
    fixture = json.loads(FIXTURE.read_text())
    result = fixture['queries']
    if single:
        result['MON N.flow;'][0]['output'] = fixture['singleFlowMonitor']
        app = result['DESCRIBE server_primary;'][0]['output'][0]['deployedObjects'][0]
        app['components'] = [c for c in app['components'] if c['entityType'] == 'APPLICATION']
    return result


def client_for(data):
    class API:
        def post_tungsten_line(self, command, timeout):
            assert timeout > 0
            return copy.deepcopy(data[command])
    return StriimClient(API())


def test_real_monitor_without_status_resolves_two_running_owners():
    data = replies()
    assert 'status' not in data['MON N.flow;'][0]['output']
    assert node_fault.flow_nodes(client_for(data), 'N.flow', 2) == {'server_primary', 'server_secondary'}


def test_single_owner_control_from_real_reply_shape():
    data = replies(single=True)
    assert node_fault.flow_nodes(client_for(data), 'N.flow', 2) == {'server_secondary'}


@pytest.mark.parametrize('status', ['STOPPED', 'DEPLOYED', 'HALT'])
def test_deployed_flow_is_not_a_running_owner(status):
    data = replies(single=True)
    data['STATUS server_secondary;'][0]['output']['applications'][0]['status'] = status
    assert node_fault.flow_nodes(client_for(data), 'N.flow', 2) == set()


def test_wrong_server_runtime_reply_cannot_prove_ownership():
    data = replies(single=True)
    data['STATUS server_secondary;'][0]['output']['server'] = 'server_primary'
    with pytest.raises(ValueError, match='placement-node-status-unavailable'):
        node_fault.flow_nodes(client_for(data), 'N.flow', 2)


def test_missing_parent_application_is_not_guessed_from_a_bare_name():
    data = replies(single=True)
    app = data['DESCRIBE server_secondary;'][0]['output'][0]['deployedObjects'][0]
    app['components'] = [c for c in app['components'] if c['entityType'] != 'APPLICATION']
    with pytest.raises(ValueError, match='placement-application-unavailable'):
        node_fault.flow_nodes(client_for(data), 'N.flow', 2)


def test_two_running_owners_still_refuse_a_docker_fault():
    data = replies()
    nodes = {r: {'role': r, 'server': 'server_' + r, 'id': 'id_' + r}
             for r in ['primary', 'secondary', 'agent']}
    records = []
    with pytest.raises(recovery.RecoveryError, match='fault-missed'):
        node_fault.Target(client_for(data), SimpleNamespace(mode='docker'),
            {'hosting': 'N.flow'}, app='N.app', nodes=nodes, record=records.append, timeout=2)
    row = records[0]['actual']['rows'][0]
    assert row['owners'] == ['server_primary', 'server_secondary']
    assert row['interrupted'] is False
    assert set(row['reads']['statuses']) == {'server_primary', 'server_secondary'}


@pytest.mark.parametrize('field,value', [('entityType', 'TARGET'), ('fullName', 'N.other')])
def test_foreign_or_nonflow_monitor_cannot_prove_ownership(field, value):
    data = replies(single=True)
    data['MON N.flow;'][0]['output'][field] = value
    with pytest.raises(ValueError, match='placement-flow-monitor-unavailable'):
        node_fault.flow_nodes(client_for(data), 'N.flow', 2)


def test_missing_runtime_application_is_unreadable_not_running():
    data = replies(single=True)
    data['STATUS server_secondary;'][0]['output']['applications'] = []
    with pytest.raises(ValueError, match='placement-node-status-unavailable'):
        node_fault.flow_nodes(client_for(data), 'N.flow', 2)


def test_late_runtime_status_cannot_select_a_fault_owner(monkeypatch):
    data = replies(single=True); clock = [0]
    monkeypatch.setattr(node_fault.time, 'monotonic', lambda: clock[0])
    class API:
        def post_tungsten_line(self, command, timeout):
            if command.startswith('STATUS'): clock[0] = 3
            return copy.deepcopy(data[command])
    with pytest.raises(ValueError, match='placement-read-deadline'):
        node_fault.flow_nodes(StriimClient(API()), 'N.flow', 2)


@pytest.mark.parametrize('reply', [[], [{'executionStatus': 'Failure', 'failureMessage': 'not available'}]])
def test_failed_runtime_status_has_a_stable_code(reply):
    from livetest.striim import StriimError
    data = replies(single=True); data['STATUS server_secondary;'] = reply
    with pytest.raises(StriimError, match='placement-node-status-unavailable'):
        node_fault.flow_nodes(client_for(data), 'N.flow', 2)
