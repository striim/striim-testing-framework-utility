"""A joined cluster must still have full membership when its topology is cached."""
from types import SimpleNamespace
import pytest
from filelock import FileLock
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
            running="5.4.2", before=None, config=None):
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
    config = config or SimpleNamespace(option=SimpleNamespace(xmlpath=xmlpath))
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


def _cold(monkeypatch, cluster_up):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(OSError("closed")))
    monkeypatch.setattr(sp, "ensure_deps", lambda *a, **k: None)
    monkeypatch.setattr(sp, "ensure_image", lambda *a, **k: None)
    monkeypatch.setattr(sp, "cluster_up", cluster_up)


def test_an_interrupted_cluster_up_leaves_the_cluster_queued_for_teardown(monkeypatch, tmp_path):
    # Ctrl-C inside `docker compose up`: the containers it created must still be torn down at session end.
    def interrupted(*a, **k):
        raise KeyboardInterrupt
    _cold(monkeypatch, interrupted)
    config = SimpleNamespace(option=SimpleNamespace(xmlpath=None))
    with pytest.raises(KeyboardInterrupt):
        resolve(monkeypatch, tmp_path, [groups(True)], docker=False, reachable=False, config=config)
    assert config._slt_striim_provisioned is True


def test_a_failed_cluster_up_is_not_recorded_as_provisioned(monkeypatch, tmp_path):
    def refused(*a, **k):
        raise RuntimeError("compose up failed")
    _cold(monkeypatch, refused)
    ctx, config, *_ = resolve(monkeypatch, tmp_path, [groups(True)], docker=False, reachable=False)
    assert ctx is None and "compose up failed" in config._slt_striim_reason
    assert getattr(config, "_slt_striim_provisioned", False) is False


def test_a_waiter_for_the_cluster_provisioning_lock_gives_up_naming_it(monkeypatch, tmp_path):
    from livetest import dockercmd
    monkeypatch.setattr(dockercmd, "LOCK_WAIT_S", 0.3)
    _cold(monkeypatch, lambda *a, **k: pytest.fail("provisioned without the lock"))
    stop = _held_elsewhere(tmp_path / "cluster.lock")
    try:
        box = _waits_at_most(lambda: resolve(monkeypatch, tmp_path, [groups(True)], docker=False, reachable=False))
    finally:
        stop()
    assert "error" not in box, box
    ctx, config, *_ = box["value"]
    assert ctx is None and "cluster.lock" in config._slt_striim_reason
    assert "not acquired within 0s" in config._slt_striim_reason


def _held_elsewhere(path):
    """Hold ``path``'s lock from another thread (a real contending holder) until the returned stop() runs."""
    import threading
    got, done = threading.Event(), threading.Event()

    def hold():
        lock = FileLock(str(path))
        lock.acquire()
        got.set()
        done.wait(60)
        lock.release()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert got.wait(10)

    def stop():
        done.set()
        t.join(10)
    return stop


def _waits_at_most(fn, seconds=10):
    """Run ``fn`` in a thread; fail if it is still waiting after ``seconds`` (an unbounded wait)."""
    import threading
    box = {}

    def call():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 - handed back to the test
            box["error"] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"still waiting for the lock after {seconds}s"
    return box


@pytest.mark.parametrize("budget", ["12", "40"])
def test_own_cluster_that_never_authenticates_is_waited_for_slt_striim_boot_wait(monkeypatch, tmp_path, budget):
    """SLT_STRIIM_BOOT_WAIT (default 300s): how long our own booting Docker cluster may take to
    authenticate before the run gives up on it."""
    monkeypatch.setenv("SLT_STRIIM_BOOT_WAIT", budget)
    monkeypatch.setattr(plugin.stack, "striim_container", lambda: "slt-striim")
    ctx, config, clock, calls, progress = resolve(monkeypatch, tmp_path, [groups(False)], reachable=False)
    assert ctx is None and calls == []
    assert f"slt-striim is running but did not authenticate within {budget}s" in config._slt_striim_reason
    assert int(budget) <= clock.now < 300, "waited its own budget, not the 300s default"
