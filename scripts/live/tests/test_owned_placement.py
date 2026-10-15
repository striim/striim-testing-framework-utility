"""Owned deployment groups isolate initial placement without excluding the survivor."""
from types import SimpleNamespace
import pytest
from livetest import ownership, runident, node_fault
from livetest.manifest import load_manifest, ManifestError


def ledger(tmp_path):
    ident = runident.derive("placement", {"SLT_RUN_EPOCH": "run"}, attempt="abcd1234")
    return ownership.Ledger(tmp_path / "ledger.json", ident, {"entries": [], "bindings": {}})


class Client:
    def __init__(self): self.groups = {}; self.commands = []; self.fail_create = False
    def list_deployment_groups(self, timeout=None):
        return [{"executionStatus": "Success", "output": [{"group": {
            "name": name, "actualServers": [{"name": n} for n in servers]}}
            for name, servers in self.groups.items()]}]
    def deploy_tql(self, text):
        self.commands.append(text)
        import re
        name = re.search(r"(?:CREATE|ALTER|DROP) DG ([A-Za-z_][A-Za-z0-9_]*)", text)[1]
        servers = re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', text)
        if "CREATE DG" in text:
            self.groups[name] = set(servers)
            if self.fail_create:
                from livetest.striim import StriimTimeout
                raise StriimTimeout("response lost")
        elif "ALTER DG" in text: self.groups[name].update(servers)
        else: self.groups.pop(name, None)


def test_group_is_confirmed_then_expanded_and_deleted_by_exact_name(tmp_path):
    l = ledger(tmp_path); c = Client(); name = l.ident.tid + "workers"
    l.acquire_deployment_group(c, name, ["server_secondary"])
    assert l.entries[0]["state"] == "confirmed"
    assert c.groups[name] == {"server_secondary"}
    l.expand_deployment_group(c, name, ["server_primary"])
    assert c.groups[name] == {"server_primary", "server_secondary"}
    l._delete(l.entries[0], client=c, admins={}, ctx=None, tokens={}, fault=False)
    assert l._verify(l.entries[0], client=c, admins={}, ctx=None) is True
    assert list(c.groups) == []
    assert ownership.CLEANUP_ORDER.index("namespace") < ownership.CLEANUP_ORDER.index("deployment-group")


def test_collision_and_foreign_name_are_never_modified(tmp_path):
    l = ledger(tmp_path); c = Client(); name = l.ident.tid + "workers"
    c.groups[name] = {"server_primary"}
    with pytest.raises(ownership.OwnershipError, match="collision:deployment-group"):
        l.acquire_deployment_group(c, name, ["server_secondary"])
    with pytest.raises(ownership.OwnershipError, match="invalid-deployment-group"):
        l.acquire_deployment_group(c, "foreign", ["server_secondary"])
    assert c.commands == [] and l.entries == []


def test_failed_create_readback_preserves_cleanup_authority(tmp_path):
    l = ledger(tmp_path); c = Client(); c.fail_create = True
    from livetest.striim import StriimTimeout
    with pytest.raises(StriimTimeout, match="response lost"):
        l.acquire_deployment_group(c, l.ident.tid + "workers", ["server_secondary"])
    assert l.entries[0]["state"] == "confirmed"


def test_unreadable_catalog_does_not_mean_absent(tmp_path):
    l = ledger(tmp_path); c = Client(); c.list_deployment_groups = lambda **kw: []
    with pytest.raises(ownership.OwnershipError, match="deployment-group-catalog-unparseable"):
        l.acquire_deployment_group(c, l.ident.tid + "workers", ["server_secondary"])
    assert c.commands == []


def test_unowned_expansion_and_changed_endpoint_are_refused(tmp_path):
    l = ledger(tmp_path); c = Client(); name = l.ident.tid + "workers"
    with pytest.raises(ownership.OwnershipError, match="deployment-group-not-owned"):
        l.expand_deployment_group(c, name, ["server_primary"])
    e = l.add("deployment-group", name, state="confirmed")
    l.doc["bindings"][l.ident.attempt] = {"striim": {"url": "old"}}
    assert "changed" in l.target_problem(e, {"striim": {"url": "new"}})


@pytest.mark.parametrize("selection", ["primary", {"hosting": "N.flow"}])
def test_metadata_primary_is_refused_before_fault(selection):
    from tests.test_node_faults import nodes, Client as FaultClient
    ns = nodes(); ns["primary"]["hostsMetadata"] = True; commands = []; records = []
    with pytest.raises(node_fault.RecoveryError, match="fault-node-hosts-metadata"):
        node_fault.Target(FaultClient(), SimpleNamespace(mode="docker"), selection, app="N.app",
            nodes=ns, placement=lambda *a: {"server_primary"}, record=records.append,
            run=lambda argv: commands.append(argv))
    assert commands == [] and records[-1]["status"] == "failed"
    assert records[-1]["actual"]["rows"][0]["interrupted"] is False


def test_group_manifest_and_reserved_tokens(tmp_path):
    p = tmp_path / "test.yaml"; (tmp_path / "app.tql").write_text("CREATE NAMESPACE ${NS};")
    p.write_text("name: placement\npurpose: Place the worker on the secondary.\ntql: app.tql\nassert: {smoke: true}\n"
        "deployment_groups:\n- name: ${TID}workers\n  flow: ${NS}.flow\n  servers: ['${STRIIM_SECONDARY_SERVER}']\n"
        "  add_after_deploy: ['${STRIIM_PRIMARY_SERVER}']\n")
    m = load_manifest(p)
    assert m.deployment_groups[0]["add_after_deploy"] == ["${STRIIM_PRIMARY_SERVER}"]
    p.write_text(p.read_text() + "tokens: {STRIIM_SECONDARY_SERVER: fake}\n")
    with pytest.raises(ManifestError, match="reserved|harness|shadow"):
        load_manifest(p)


@pytest.mark.parametrize("block", ["{}", "[{name: x, servers: [s], typo: true}]",
    "[{name: x, servers: []}]", "[{name: x, servers: [s], add_after_deploy: false}]"])
def test_invalid_group_manifest_has_stable_code(tmp_path, block):
    p = tmp_path / "test.yaml"; (tmp_path / "app.tql").write_text("CREATE NAMESPACE ${NS};")
    p.write_text("name: placement\npurpose: Check placement.\ntql: app.tql\nassert: {smoke: true}\ndeployment_groups: " + block)
    with pytest.raises(ManifestError, match="invalid-deployment-group"):
        load_manifest(p)
