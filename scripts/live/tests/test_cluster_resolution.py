"""A joined cluster must still have full membership when its topology is cached."""
from types import SimpleNamespace
import pytest
from livetest import plugin, striim_provision as sp
from livetest.topology import topology_satisfies


def groups(cluster):
    return [{"output": [
        {"app": {"name": "default", "actualServers": [{"uuid": "u1"}] + ([{"uuid": "u2"}] if cluster else [])}},
        {"source": {"name": "Agents", "actualServers": [{"uuid": "a1"}]}}]}]


class Clock:
    def __init__(self): self.now = 0
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


def resolve(monkeypatch, tmp_path, responses, *, docker=True, xmlpath=None):
    clock, calls, progress = Clock(), [], []
    class Client:
        api = SimpleNamespace(post_tungsten_line=lambda *args, **kwargs: None)
        def list_deployment_groups(self, timeout=None):
            calls.append((clock.now, timeout))
            return responses.pop(0) if len(responses) > 1 else responses[0]
    monkeypatch.setattr(plugin.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(plugin.time, "sleep", clock.sleep)
    monkeypatch.setattr(plugin.paths, "setting", lambda key: "http://example.invalid:9080" if key == "STRIIM_URL" else None)
    monkeypatch.setenv("SLT_CLUSTER_SETTLE", "20")
    monkeypatch.setattr(plugin, "probe_reachable", lambda *args: True)
    monkeypatch.setattr(plugin, "_striim5_running", lambda: docker)
    monkeypatch.setattr(plugin, "_running_striim_version", lambda: "5.4.2")
    monkeypatch.setattr(plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": "5.4.2"})
    monkeypatch.setattr(plugin, "_redeploy_reason", lambda *args: None)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda: tmp_path / "cluster.lock")
    monkeypatch.setattr(plugin, "_make_progress", lambda config: (lambda *args: progress.append(args), lambda: None))
    monkeypatch.setattr(plugin, "StriimClient", SimpleNamespace(from_url=lambda *args: Client()))
    monkeypatch.setattr(plugin._slt_infra, "before_cluster_resolution", lambda *args: None)
    monkeypatch.setattr(plugin._slt_infra, "bind_striim", lambda *args: None)
    monkeypatch.setattr(sp, "verify_current_inputs", lambda *args: None)
    config = SimpleNamespace(option=SimpleNamespace(xmlpath=xmlpath))
    return plugin._resolve_striim(config), config, clock, calls, progress


def test_registered_cluster_with_transient_post_settle_membership_is_rechecked(monkeypatch, tmp_path):
    ctx, config, clock, calls, progress = resolve(monkeypatch, tmp_path,
        [groups(False), groups(True), groups(False), groups(True)])
    assert ctx is not None
    assert topology_satisfies("cluster", ctx.topology)[0]
    assert ctx.topology.groups == {"default": ["u1", "u2"], "Agents": ["a1"]}
    assert len(calls) == 4 and calls[-1][0] >= 30
    assert plugin._resolve_striim(config) is ctx and len(calls) == 4


def test_registration_then_persistent_missing_node_never_qualifies_cluster(monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(sp, "save_cluster_logs", lambda path, *args: saved.append(path) or path)
    ctx, config, clock, calls, progress = resolve(monkeypatch, tmp_path,
        [groups(False), groups(True), groups(False)], xmlpath=tmp_path / 'live' / 'junit.xml')
    assert ctx is None and "CLUSTER-READY" in config._slt_striim_reason
    assert 'default=1, Agents=1' in config._slt_striim_reason
    assert saved == [tmp_path / 'live' / 'cluster-logs']
    assert 'node logs saved' in config._slt_striim_reason
    assert clock.now <= 320
    assert any("node + agent" in str(message) for message in progress)


def test_native_single_node_does_not_wait_for_docker_membership(monkeypatch, tmp_path):
    ctx, config, clock, calls, progress = resolve(monkeypatch, tmp_path, [groups(False)], docker=False)
    assert ctx.mode == "native" and not ctx.topology.has_cluster
    assert topology_satisfies("single", ctx.topology)[0]
    assert len(calls) == 1 and clock.now == 0


def test_late_membership_read_cannot_prove_cluster_ready(monkeypatch):
    clock, calls = Clock(), []
    monkeypatch.setattr(sp.time, 'monotonic', clock.monotonic)
    class Client:
        def list_deployment_groups(self, timeout=None):
            calls.append(timeout)
            clock.now += 2
            return groups(True)
    with pytest.raises(TimeoutError, match='CLUSTER-READY'):
        sp.wait_cluster_ready(Client(), timeout=1, poll=0)
    assert calls == [1]
