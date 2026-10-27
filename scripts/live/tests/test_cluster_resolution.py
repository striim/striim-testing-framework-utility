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


def resolve(monkeypatch, tmp_path, responses, *, docker=True, xmlpath=None, reachable=True, probes=None,
            running="5.4.2", before=None):
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
    answers = iter(probes or ())
    monkeypatch.setattr(plugin, "probe_reachable", lambda *args: next(answers, reachable))
    monkeypatch.setattr(plugin, "_striim5_running", lambda: docker)
    monkeypatch.setattr(plugin, "_running_striim_version", lambda: running)
    monkeypatch.setattr(plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": "5.4.2"})
    monkeypatch.setattr(plugin, "_redeploy_reason", lambda *args: None)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda: tmp_path / "cluster.lock")
    monkeypatch.setattr(plugin, "_make_progress", lambda config: (lambda *args: progress.append(args), lambda: None))
    monkeypatch.setattr(plugin, "StriimClient", SimpleNamespace(from_url=lambda *args: Client()))
    monkeypatch.setattr(plugin._slt_infra, "before_cluster_resolution", lambda *args: None)
    monkeypatch.setattr(plugin._slt_infra, "bind_striim", lambda *args: None)
    monkeypatch.setattr(sp, "verify_current_inputs", lambda *args: None)
    if before:
        before()
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


# --- tryout jars (livetest.tryout) -------------------------------------------------------------

def _tryouts(monkeypatch, tmp_path):
    jar = tmp_path / "Platform-1.0.0-SNAPSHOT.jar"
    import zipfile
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/maven/com.webaction/Platform/pom.properties", "artifactId=Platform\n")
    monkeypatch.setenv(plugin._tryout.TRYOUT_ENV, f"both:{jar}")


def test_a_cluster_whose_tryouts_do_not_verify_fails_every_case(monkeypatch, tmp_path):
    _tryouts(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_verify_tryouts", lambda config, tryouts: "tryout jars not verified: mismatch")
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)])
    assert ctx is None and config._slt_striim_hard and "mismatch" in config._slt_striim_reason
    with pytest.raises(pytest.fail.Exception, match="mismatch"):
        plugin._fail_on_hard_striim_failure(config, ctx)


def test_verified_tryouts_resolve_the_cluster(monkeypatch, tmp_path):
    _tryouts(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(plugin, "_verify_tryouts", lambda config, tryouts: seen.append(tryouts) and None)
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)])
    assert ctx is not None and len(seen[0]) == 1 and not getattr(config, "_slt_striim_hard", False)
    plugin._fail_on_hard_striim_failure(config, ctx)              # nothing to fail


def test_tryouts_against_a_native_striim_fail_rather_than_skip(monkeypatch, tmp_path):
    _tryouts(monkeypatch, tmp_path)
    monkeypatch.setattr(plugin, "_verify_tryouts", lambda *a: pytest.fail("verified a native Striim"))
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(False)], docker=False)
    assert ctx is None and config._slt_striim_hard and "needs the Docker cluster" in config._slt_striim_reason


def test_a_plain_unreachable_cluster_still_skips(monkeypatch, tmp_path):
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(False), groups(True), groups(False)])
    assert ctx is None and not getattr(config, "_slt_striim_hard", False)
    plugin._fail_on_hard_striim_failure(config, ctx)              # a skip, decided by the caller


def test_a_tryout_that_cannot_be_built_fails_every_case(monkeypatch, tmp_path):
    import socket
    _tryouts(monkeypatch, tmp_path)

    def no_jar(*args, **kwargs):
        raise plugin._tryout.TryoutError("tryout Platform-1.0.0-SNAPSHOT.jar matches no jar in /opt/striim/lib")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr(sp, "ensure_deps", lambda *a, **k: None)
    monkeypatch.setattr(sp, "ensure_image", no_jar)
    monkeypatch.setattr(sp, "cluster_up", lambda *a, **k: pytest.fail("started a cluster"))
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)], docker=False, reachable=False)
    assert ctx is None and config._slt_striim_hard and "matches no jar" in config._slt_striim_reason



def test_setting_off_redeploys_a_booting_tryout_cluster_before_accepting_it(monkeypatch, tmp_path):
    """The boot-wait branch, undeclared and not in candidate mode: three failed logins, then the booting cluster
    answers. It still runs a tryout image while the setting is unset, so it is redeployed onto the stock image
    before the context is accepted -- never accepted and recorded as a stock run."""
    from livetest import infra
    events = []
    monkeypatch.delenv(plugin._tryout.TRYOUT_ENV, raising=False)
    monkeypatch.setattr(sp, "candidate_mode", lambda: False)

    def wire():
        monkeypatch.setattr(sp, "cluster_down", lambda *a, **k: events.append("down"))
        monkeypatch.setattr(sp, "ensure_deps", lambda *a, **k: events.append("deps"))
        monkeypatch.setattr(sp, "ensure_image", lambda *a, **k: events.append("image"))
        monkeypatch.setattr(sp, "cluster_up", lambda *a, **k: events.append("up"))
        monkeypatch.setattr(sp, "tryout_stock_check", lambda: (None, []))     # the redeployed cluster: stock
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)], probes=[False, False, False, True],
                              running="5.4.2-tryout-0123456789ab", before=wire)
    assert events == ["down", "deps", "image", "up"]
    assert ctx is not None and config._slt_striim_provisioned is True
    assert infra.observe_tryouts(config) == []


def test_a_booting_cluster_on_the_stock_image_is_reused_as_before(monkeypatch, tmp_path):
    events = []
    monkeypatch.delenv(plugin._tryout.TRYOUT_ENV, raising=False)
    monkeypatch.setattr(sp, "candidate_mode", lambda: False)

    def wire():
        monkeypatch.setattr(sp, "cluster_down", lambda *a, **k: events.append("down"))
        monkeypatch.setattr(sp, "tryout_stock_check", lambda: (None, []))
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)], probes=[False, False, False, True],
                              before=wire)
    assert ctx is not None and events == [] and config._slt_striim_provisioned is False
