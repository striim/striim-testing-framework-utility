from types import SimpleNamespace
import pytest
from livetest.manifest import _normalize_recover, ManifestError
from livetest import recovery


@pytest.mark.parametrize("mode", ["stop", "kill", "restart"])
def test_named_node_modes_are_loaded(mode):
    result = _normalize_recover({"mode": mode, "node": "primary"}, "case")
    assert result["node"] == "primary"


@pytest.mark.parametrize("node", [True, "foreign", {}, {"hosting": ""}, {"hosting": "N.flow", "extra": 1}])
def test_unknown_or_ambiguous_node_refused(node):
    with pytest.raises(ManifestError, match="invalid-node-fault"):
        _normalize_recover({"mode": "kill", "node": node}, "case")


def test_quiesce_does_not_mean_node_stop():
    with pytest.raises(ManifestError, match="invalid-node-fault"):
        _normalize_recover({"mode": "quiesce", "node": "primary"}, "case")


def test_native_named_node_fault_is_refused():
    ok, reason = recovery.supported("stop", SimpleNamespace(mode="native"), node="primary")
    assert ok is False and "Docker" in reason


def nodes():
    return {role: {"role": role, "container": f"container_{role}", "id": f"id_{role}",
                   "server": f"server_{role}", "url": f"endpoint_{role}"}
            for role in ("primary", "secondary", "agent")}


class Client:
    def __init__(self): self.fallbacks = None
    def configure_failover(self, urls, app=None): self.fallbacks = urls
    def await_running(self, app, timeout, **kw): pass
    def current_status(self, app, **kw): return "RUNNING"


@pytest.mark.parametrize("role", ["primary", "secondary", "agent"])
@pytest.mark.parametrize("mode", ["stop", "kill", "restart"])
def test_only_selected_container_is_interrupted(monkeypatch, role, mode):
    from livetest import node_fault
    commands, records = [], []
    def run(argv):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="true" if mode == "restart" else "false", stderr="")
    target = node_fault.Target(Client(), SimpleNamespace(mode="docker"), role, app="N.app",
        nodes=nodes(), run=run, record=records.append, timeout=1)
    target.interrupt(mode)
    assert commands[0] == (["docker", "kill", "-s", "KILL", f"id_{role}"] if mode == "kill"
                           else ["docker", mode, f"id_{role}"])
    assert all("id_" + other not in argv for argv in commands for other in nodes() if other != role)
    assert records[0]["status"] == "passed"


def test_hosting_selector_uses_unique_running_flow_owner():
    from livetest import node_fault
    client = Client()
    target = node_fault.Target(client, SimpleNamespace(mode="docker"), {"hosting": "N.flow"}, app="N.app",
        nodes=nodes(), placement=lambda flow, left: {"server_secondary"}, run=lambda a: None, timeout=1)
    assert target.selected["role"] == "secondary"
    assert client.fallbacks == ["endpoint_primary"]


def test_moved_flow_refused_before_command():
    from livetest import node_fault
    owner = {"server_primary"}; commands = []
    target = node_fault.Target(Client(), SimpleNamespace(mode="docker"), {"hosting": "N.flow"}, app="N.app",
        nodes=nodes(), placement=lambda flow, left: set(owner), run=lambda a: commands.append(a), timeout=1)
    owner.clear(); owner.add("server_secondary")
    with pytest.raises(recovery.RecoveryError, match="fault-missed"):
        target.check_owner(1)
    assert not commands


def test_failed_kill_never_records_a_passed_fault():
    from livetest import node_fault
    records = []
    target = node_fault.Target(Client(), SimpleNamespace(mode="docker"), "agent", app="N.app",
        nodes=nodes(), run=lambda a: SimpleNamespace(returncode=1, stdout="", stderr="rejected"),
        record=records.append, timeout=1)
    with pytest.raises(recovery.RecoveryError): target.interrupt("kill")
    assert records[-1]["status"] == "failed"


def test_restore_starts_only_failed_node_after_survivor_placement(monkeypatch):
    from livetest import node_fault
    commands, owner = [], {"server_primary"}
    def run(argv):
        commands.append(argv)
        if argv[1] == "kill": owner.clear(); owner.add("server_secondary")
        return SimpleNamespace(returncode=0, stdout="false", stderr="")
    monkeypatch.setattr(node_fault, "_reset_modules", lambda: None)
    target = node_fault.Target(Client(), SimpleNamespace(mode="docker"), {"hosting": "N.flow"}, app="N.app",
        nodes=nodes(), placement=lambda flow, left: set(owner), run=run, timeout=1,
        wait_cluster=lambda *a, **k: None)
    target.interrupt("kill"); target.restore(expect_running=True)
    assert [a for a in commands if a[1] == "start"] == [["docker", "start", "id_primary"]]
    target.close()
    assert len([a for a in commands if a[1] == "start"]) == 1


def test_flow_descriptor_does_not_match_prefix_neighbor():
    from livetest.node_fault import deployed_flow
    data = [{"name": "server_primary", "deployedObjects": [{"applicationName": "N.app",
        "components": [{"entityType": "FLOW", "name": "N.flow2"}]}]}]
    assert deployed_flow(data, "N.flow") is False
    data[0]["deployedObjects"][0]["components"].append({"entityType": "FLOW", "name": "N.flow"})
    assert deployed_flow(data, "N.flow") is True
    with pytest.raises(ValueError): deployed_flow([{"name": "server_primary"}], "N.flow")


def test_inventory_maps_live_names_and_published_peer_port(monkeypatch):
    import json
    from livetest import node_fault
    monkeypatch.setattr(node_fault.stack, "striim_container", lambda: "primary")
    monkeypatch.setattr(node_fault.stack, "app_nodes", lambda: ["primary", "secondary"])
    monkeypatch.setattr(node_fault.stack, "agent_container", lambda: "agent")
    class Metadata:
        def list_deployment_groups(self, timeout):
            return [{"output": [{"default": {"actualServers": [
                {"name": "server_" + role} for role in nodes()]}}]}]
    def inspect(argv):
        role = argv[-1]
        row = {"Id": "id_" + role, "State": {"Running": True}, "Config": {"Env": [
            "JAVA_SYSTEM_PROPERTIES=-Dstriim.node.server.name=server_" + role]},
            "NetworkSettings": {"Ports": {"9080/tcp": [{"HostPort": "19082"}]}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps([row]))
    result = node_fault.inventory(Metadata(), SimpleNamespace(mode="docker", url="http://test.invalid:19080"), run=inspect)
    assert result["secondary"]["url"] == "http://test.invalid:19082"
    assert result["agent"]["url"] is None
    assert result["primary"]["server"] == "server_primary"


def test_secondary_api_port_is_published_and_allowed():
    import yaml
    from pathlib import Path
    from livetest import paths
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "scripts/live/services/striim/compose.yaml").read_text())
    assert "${SLT_STRIIM_NODE_HTTP_HOST_PORT:-9082}:9080" in compose["services"]["slt-node"]["ports"]
    assert "SLT_STRIIM_NODE_HTTP_HOST_PORT" in paths.SERVICE_KEYS
