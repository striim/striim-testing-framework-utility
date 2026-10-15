"""Exercise owned placement through the real plugin and persisted cleanup ledger."""
import pytest
from .exec_harness import run_case  # noqa: F401

pytest_plugins = ["pytester"]


@pytest.mark.parametrize("drop_error", [False, True])
def test_secondary_is_proven_and_survivor_added_before_post_start_seed(run_case, drop_error):
    def prepare(root, name):
        with (root / "conftest.py").open("a") as f:
            f.write(r'''
from livetest import node_fault
GROUPS = {}
def group_inventory(self, timeout=None):
    return [{"executionStatus": "Success", "output": [{"group": {
        "name": n, "actualServers": [{"name": s} for s in members]}}
        for n, members in GROUPS.items()]}]
def group_tql(self, text):
    name = re.search(r"(?:CREATE|ALTER|DROP) DG ([A-Za-z_][A-Za-z0-9_]*)", text)[1]
    servers = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', text))
    if 'CREATE DG' in text:
        log('group_create'); GROUPS[name] = servers
    elif 'ALTER DG' in text:
        log('group_expand'); GROUPS[name].update(servers)
    else:
        assert not NAMESPACES
        log('group_drop')
        if SC.get('drop_error'): raise RuntimeError('group drop rejected')
        del GROUPS[name]
FakeClient.list_deployment_groups = group_inventory
FakeClient.deploy_tql = group_tql
node_fault.inventory = lambda *a, **k: {r: {'server': 'server_' + r} for r in ('primary', 'secondary', 'agent')}
def placement(*a, **k):
    assert list(GROUPS.values()) == [{'server_secondary'}]
    log('initial_owner'); return {'server_secondary'}
node_fault.flow_nodes = placement
''')
    run = run_case("cdc-sentinel", {"mirror": "live", "drop_error": drop_error}, prepare=prepare, edit=lambda s: s + """
deployment_groups:
- name: ${TID}workers
  flow: ${NS}.${TID}flow
  servers: ['${STRIIM_SECONDARY_SERVER}']
  add_after_deploy: ['${STRIIM_PRIMARY_SERVER}']
""")
    assert run.ret == (1 if drop_error else 0), run.text
    names = run.names()
    assert names.index("group_create") < names.index("deploy") < names.index("initial_owner")
    assert names.index("initial_owner") < names.index("group_expand") < names.index("insert")
    assert names.index("teardown_namespace") < names.index("group_drop")
    env = run.envelope()
    from livetest import evidence
    evidence.validate_case(env, secrets=())
    assert env["resources"]["cleanupVerified"] is not drop_error
    assert env["lifecycle"]["cleanup"]["status"] == ("failed" if drop_error else "ok")
    assert any(e["kind"] == "deployment-group" and e["state"] == ("delete-failed" if drop_error else "verified-absent")
               for e in env["resources"]["owned"])
