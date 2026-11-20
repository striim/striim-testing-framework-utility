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
            "JAVA_SYSTEM_PROPERTIES=-Dstriim.node.server.name=server_" + role,
            "ROLE=" + ("primary" if role == "primary" else "node" if role == "secondary" else "agent")]},
            "NetworkSettings": {"Ports": {"9080/tcp": [{"HostPort": "19082"}]}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps([row]))
    result = node_fault.inventory(Metadata(), SimpleNamespace(mode="docker", url="http://test.invalid:19080"), run=inspect)
    assert result["secondary"]["url"] == "http://test.invalid:19082"
    assert result["agent"]["url"] is None
    assert result["primary"]["server"] == "server_primary"
    assert result["primary"]["hostsMetadata"] is True
    assert result["secondary"]["hostsMetadata"] is False


def test_secondary_api_port_is_opt_in_and_allowed(monkeypatch):
    import yaml
    from pathlib import Path
    from livetest import paths
    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "scripts/live/services/striim/compose.yaml").read_text())
    from livetest import striim_provision as sp
    mapping = "${SLT_STRIIM_NODE_HTTP_HOST_PORT:-9082}:9080"
    bound = [f"{b}:{mapping}" for b in ("${SLT_BIND_ADDRESS:-0.0.0.0}",
                                        "${SLT_BIND_GATEWAY:-${SLT_BIND_ADDRESS:-0.0.0.0}}")]
    assert not [p for p in compose["services"]["slt-node"]["ports"] if p.endswith(mapping)]
    recipe = root / "scripts/live/services/striim"
    monkeypatch.delenv("SLT_ENABLE_NODE_FAULTS", raising=False)
    assert "compose.node-fault.yaml" not in sp._compose_files()
    monkeypatch.setenv("SLT_ENABLE_NODE_FAULTS", "1")
    assert "compose.node-fault.yaml" in sp._compose_files()
    overlay = yaml.safe_load((recipe / "compose.node-fault.yaml").read_text())
    assert overlay["services"]["slt-node"]["ports"] == bound
    assert "SLT_STRIIM_NODE_HTTP_HOST_PORT" in paths.SERVICE_KEYS


def placement_client(*, owners=(), status='RUNNING', malformed=None):
    class Metadata(Client):
        def list_deployment_groups(self, timeout):
            return [{'output': [{'default': {'actualServers': [
                {'name': 'server_primary'}, {'name': 'server_secondary'}]},
                'Agents': {'actualServers': [{'name': 'server_agent'}]}}]}]
        def mon(self, flow, timeout):
            return {'entityType': 'FLOW', 'fullName': flow, 'numServers': str(len(owners)),
                    'flowComponents': [], 'detail': {'reason': 'observed'}}
        def node_status(self, name, timeout):
            return {'server': name, 'applications': [{'app': 'N.app', 'status': status}]}
        def describe(self, name, timeout):
            if malformed == name: return [{'name': name, 'deployedObjects': 'unrecognised'}]
            return [{'name': name, 'deployedObjects': [{'name': 'N.app', 'components':
                [{'entityType': 'FLOW', 'name': 'N.flow'}, {'entityType': 'APPLICATION', 'name': 'N.app'}] if name in owners else []}]}]
    return Metadata()


@pytest.mark.parametrize('owners', [(), ('server_primary', 'server_secondary')])
def test_refused_hosting_selection_retains_all_placement_reads(owners):
    import json
    from livetest import node_fault
    records, commands = [], []
    client = placement_client(owners=owners)
    with pytest.raises(recovery.RecoveryError, match='fault-missed'):
        node_fault.Target(client, SimpleNamespace(mode='docker'), {'hosting': 'N.flow'},
            app='N.app', nodes=nodes(), run=commands.append, record=records.append, timeout=1)
    assert not commands and len(records) == 1
    assert records[0]['status'] == 'failed'
    diagnostic = records[0]['actual']['rows'][0]
    assert diagnostic['phase'] == 'owner-selection' and diagnostic['interrupted'] is False
    assert diagnostic['owners'] == sorted(owners)
    reads = diagnostic['reads']
    assert json.loads(reads['deploymentGroups']['json']) == client.list_deployment_groups(1)
    assert json.loads(reads['monitor']['json']) == client.mon('N.flow', 1)
    assert set(reads['servers']) == {n['server'] for n in nodes().values()}
    for name, captured in reads['servers'].items():
        assert json.loads(captured['json']) == client.describe(name, 1)
        assert captured['truncated'] is False


def test_bad_placement_shape_is_retained_before_read_error():
    import json
    from livetest import node_fault
    records = []
    with pytest.raises(ValueError, match='placement-node-description-unavailable'):
        node_fault.Target(placement_client(malformed='server_primary'),
            SimpleNamespace(mode='docker'), {'hosting': 'N.flow'}, app='N.app',
            nodes=nodes(), record=records.append, timeout=1)
    diagnostic = records[0]['actual']['rows'][0]
    assert diagnostic['owners'] is None
    assert json.loads(diagnostic['reads']['servers']['server_primary']['json'])[0]['deployedObjects'] == 'unrecognised'


def test_moved_flow_refusal_retains_fault_boundary_reads():
    from livetest import node_fault
    records = []
    client = placement_client(owners=('server_primary',))
    target = node_fault.Target(client, SimpleNamespace(mode='docker'), {'hosting': 'N.flow'},
        app='N.app', nodes=nodes(), record=records.append, timeout=1)
    client.describe = placement_client(owners=('server_secondary',)).describe
    with pytest.raises(recovery.RecoveryError, match='flow moved'):
        target.check_owner(1)
    diagnostic = records[0]['actual']['rows'][0]
    assert diagnostic['phase'] == 'fault-boundary'
    assert diagnostic['owners'] == [] and diagnostic['selected'] == 'server_primary'
    assert set(diagnostic['reads']['servers']) == {'server_primary'}


def test_owner_read_evidence_is_bounded_and_redacted(monkeypatch):
    from livetest import node_fault, evidence
    # This checks serialization bounds, independently of transport/read deadlines.
    monkeypatch.setattr(node_fault.time, 'monotonic', lambda: 1.0)
    monkeypatch.setattr(evidence, 'known_secrets', lambda: ('private-test-secret',))
    client = placement_client()
    client.mon = lambda *args, **kwargs: {'entityType': 'FLOW', 'fullName': 'N.flow', 'secret': 'private-test-secret',
                                        'padding': 'x' * 100000}
    records = []
    with pytest.raises(recovery.RecoveryError):
        node_fault.Target(client, SimpleNamespace(mode='docker'), {'hosting': 'N.flow'},
            app='N.app', nodes=nodes(), record=records.append, timeout=1)
    captured = records[0]['actual']['rows'][0]['reads']['monitor']
    assert captured['truncated'] and captured['characters'] > 65536
    assert len(captured['json']) == 65536
    assert 'private-test-secret' not in str(records)


@pytest.mark.parametrize("opt_in", [None, "0", "true", "1"])
def test_node_fault_gate_precedes_input_snapshot_and_provision(monkeypatch, opt_in):
    from livetest import plugin
    manifest = SimpleNamespace(topology="cluster", requires=[], disabled=False,
        disabled_parallel=False, recover={"node": "secondary"}, exact=None)
    monkeypatch.setattr(plugin, "load_manifest", lambda path: manifest)
    monkeypatch.setenv("SLT_RUN_DISABLED", "1")
    monkeypatch.delenv("SLT_ENABLE_NODE_FAULTS", raising=False)
    if opt_in is not None:
        monkeypatch.setenv("SLT_ENABLE_NODE_FAULTS", opt_in)
    calls = []
    def snapshot(*args, **kwargs):
        calls.append("snapshot")
        raise RuntimeError("snapshot reached")
    monkeypatch.setattr(plugin._slt_inputs, "snapshot", snapshot)
    monkeypatch.setattr(plugin, "_resolve_striim", lambda *a: pytest.fail("provisioning reached"))
    item = SimpleNamespace(manifest_path="case/test.yaml")
    if opt_in == "1":
        with pytest.raises(RuntimeError, match="snapshot reached"):
            plugin.LiveItem._runtest(item)
        assert calls == ["snapshot"]
    elif opt_in == "true":
        # Only 1 is on; any other value is refused (pytest_configure refuses it before any case).
        with pytest.raises(plugin.paths.FlagError, match="SLT_ENABLE_NODE_FAULTS='true'"):
            plugin.LiveItem._runtest(item)
        assert calls == []
    else:
        with pytest.raises(pytest.skip.Exception, match="SLT_ENABLE_NODE_FAULTS"):
            plugin.LiveItem._runtest(item)
        assert calls == []


@pytest.mark.parametrize("mode", ["kill", "restart"])
@pytest.mark.parametrize("rejoin_fails", [False, True])
def test_failure_cleanup_waits_for_rejoin_and_records_outcome(monkeypatch, mode, rejoin_fails):
    from livetest import node_fault
    events, records = [], []
    def run(argv):
        events.append(argv[1])
        return SimpleNamespace(returncode=0, stdout="true" if mode == "restart" else "false")
    def rejoin(client, remaining):
        events.append("rejoin")
        assert 0 < remaining <= 1
        if rejoin_fails:
            raise RuntimeError("membership deadline")
    target = node_fault.Target(Client(), SimpleNamespace(mode="docker"), "secondary", app="N.app",
        nodes=nodes(), run=run, record=records.append, timeout=1, wait_cluster=rejoin)
    target.interrupt(mode)
    if rejoin_fails:
        with pytest.raises(recovery.RecoveryError, match="membership deadline"):
            target.close()
    else:
        target.close()
        target.close()
    assert events == [mode, "inspect"] + (["start"] if mode == "kill" else []) + ["rejoin"]
    assert records[-1]["spec"]["phase"] == "cleanup-rejoin"
    assert records[-1]["status"] == ("failed" if rejoin_fails else "passed")
    assert target.needs_rejoin is rejoin_fails


# --- the last settle read is checked; a role fault records whether the app ran there ---------

class SeqClient(Client):
    """Statuses in turn (the last repeats), and a STATUS <server> listing per server."""
    def __init__(self, statuses, listing=None):
        super().__init__()
        self.statuses, self.listing, self.status_reads = list(statuses), listing or {}, []

    def current_status(self, app, **kw):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def node_status(self, name, timeout=None):
        self.status_reads.append((name, timeout))
        value = self.listing[name]
        if isinstance(value, Exception):
            raise value
        return value


def _role_target(monkeypatch, client, role="secondary", records=None, mode_state="false"):
    from livetest import node_fault
    monkeypatch.setattr(node_fault, "_reset_modules", lambda: None)
    return node_fault.Target(client, SimpleNamespace(mode="docker"), role, app="N.app", nodes=nodes(),
        run=lambda a: SimpleNamespace(returncode=0, stdout=mode_state, stderr=""),
        record=(records.append if records is not None else None), timeout=5,
        wait_cluster=lambda *a, **k: None)


@pytest.mark.parametrize("settle,statuses", [(0, ["CRASH"]), (0.05, ["RUNNING", "CRASH"])])
def test_restore_raises_on_a_terminal_status_from_the_last_settle_read(monkeypatch, settle, statuses):
    # settle 0: the one read is the returned one; settle > 0: the read after the last sleep is.
    t = _role_target(monkeypatch, SeqClient(statuses))
    with pytest.raises(recovery.RecoveryError, match="fault-node-restore-status: CRASH"):
        t.restore(expect_running=True, settle=settle)


def test_restore_returns_running_after_a_clean_settle(monkeypatch):
    t = _role_target(monkeypatch, SeqClient(["RUNNING"]))
    assert t.restore(expect_running=True, settle=0.05) == "RUNNING"


@pytest.mark.parametrize("status", ["RUNNING", "CRASH"])
def test_restore_expect_running_false_refuses_running_and_terminal(monkeypatch, status):
    records = []
    t = _role_target(monkeypatch, SeqClient([status]), records=records)
    with pytest.raises(recovery.RecoveryError, match=status):
        t.restore(expect_running=False)
    assert records[-1]["status"] == "failed" and records[-1]["actual"]["rows"][0]["status"] == status


def test_restore_expect_running_false_records_a_stopped_app(monkeypatch):
    records = []
    t = _role_target(monkeypatch, SeqClient(["DEPLOYED"]), records=records)
    assert t.restore(expect_running=False) == "DEPLOYED"
    assert records[-1]["status"] == "passed" and records[-1]["actual"]["rows"][0]["status"] == "DEPLOYED"


def _listing(server, app, status):
    return {"server": server, "applications": [{"app": app, "status": status}]}


@pytest.mark.parametrize("listing,affected,on_node", [
    (_listing("server_secondary", "N.app", "RUNNING"), True, "RUNNING"),
    (_listing("server_secondary", "N.other", "RUNNING"), False, None),
    (_listing("server_secondary", "N.app", "DEPLOYED"), False, "DEPLOYED"),
])
def test_role_fault_records_whether_the_app_runs_on_the_selected_node(monkeypatch, listing, affected, on_node):
    records = []
    client = SeqClient(["RUNNING"], listing={"server_secondary": listing})
    t = _role_target(monkeypatch, client, records=records)
    t.check_owner(5)
    t.interrupt("kill")
    row = records[-1]["actual"]["rows"][0]
    assert records[-1]["status"] == "passed"          # a role makes no claim about ownership
    assert row["appAffected"] is affected and row["appStatusOnNode"] == on_node
    assert client.status_reads and client.status_reads[0][0] == "server_secondary"
    assert client.status_reads[0][1] is not None and client.status_reads[0][1] <= 5


def test_role_fault_with_an_unreadable_listing_records_unknown_not_unaffected(monkeypatch):
    records = []
    client = SeqClient(["RUNNING"], listing={"server_agent": RuntimeError("no STATUS for agents")})
    t = _role_target(monkeypatch, client, role="agent", records=records)
    t.check_owner(5)
    t.interrupt("kill")
    row = records[-1]["actual"]["rows"][0]
    assert row["appAffected"] is None and "no STATUS for agents" in row["placementError"]


def test_hosting_fault_is_affected_by_construction():
    from livetest import node_fault
    records = []
    t = node_fault.Target(Client(), SimpleNamespace(mode="docker"), {"hosting": "N.flow"}, app="N.app",
        nodes=nodes(), placement=lambda flow, left: {"server_secondary"},
        run=lambda a: SimpleNamespace(returncode=0, stdout="false", stderr=""),
        record=records.append, timeout=1)
    t.check_owner(1)
    t.interrupt("kill")
    assert records[-1]["actual"]["rows"][0]["appAffected"] is True


# --- evidence rows carry a UTC stamp beside each monotonic reading ---------------------------

def _utc(value):
    from datetime import datetime
    stamp = datetime.fromisoformat(value)
    assert stamp.tzinfo is not None
    return stamp


def test_interrupt_row_carries_a_utc_stamp(monkeypatch):
    records = []
    t = _role_target(monkeypatch, SeqClient(["RUNNING"], listing={
        "server_secondary": _listing("server_secondary", "N.app", "RUNNING")}), records=records)
    t.check_owner(5)
    t.interrupt("kill")
    row = records[-1]["actual"]["rows"][0]
    assert isinstance(row["observedAt"], float)
    _utc(row["observedUtc"])


def test_failover_wait_rows_carry_utc_beside_started_and_deadline(monkeypatch):
    from livetest import node_fault
    records, owner = [], {"server_secondary"}
    def run(argv):
        if argv[1] == "kill": owner.clear(); owner.add("server_primary")
        return SimpleNamespace(returncode=0, stdout="false", stderr="")
    monkeypatch.setattr(node_fault, "_reset_modules", lambda: None)
    t = node_fault.Target(Client(), SimpleNamespace(mode="docker"), {"hosting": "N.flow"}, app="N.app",
        nodes=nodes(), placement=lambda flow, left: set(owner), run=run, record=records.append,
        timeout=1, wait_cluster=lambda *a, **k: None)
    t.interrupt("kill"); t.restore(expect_running=True)
    wait = next(r for r in records if r["actual"]["rows"][0].get("phase") == "failover-wait")
    row = wait["actual"]["rows"][0]
    _utc(row["deadlineUtc"])
    _utc(row["observations"][0]["startedUtc"])
    _utc(row["observations"][0]["observedUtc"])


def test_initial_placement_row_carries_utc_beside_deadline_and_observed():
    from livetest import node_fault
    records = []
    node_fault.initial_placement(client=None, flow="N.flow", servers={"S1"}, timeout=5,
        record=records.append, read=lambda f, left: {"S1"})
    row = records[0]["actual"]["rows"][0]
    deadline, observed = _utc(row["deadlineUtc"]), _utc(row["observedUtc"])
    assert 4 <= (deadline - observed).total_seconds() <= 5
