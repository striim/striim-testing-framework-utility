"""The real ``livetest.plugin._resolve_striim`` under a declared ownership
(C7.1).

Driven the way ``tests/test_striim_reuse.py`` drives it: fakes for reachability, the running
cluster, its version, provisioning and the Striim client, and a recorder for every cluster
lifecycle call. Shared never redeploys (both redeploy branches); exclusive refuses a reachable
endpoint or an open port before the provisioning lock. An undeclared config keeps today's
behaviour, which ``tests/test_striim_reuse.py`` proves unchanged.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import infra
from livetest import striim_provision as sp


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    from livetest import plugin as _plugin
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def trap(*args, **kwargs):
        pytest.fail(f"workload subprocess attempted: {args[:1]!r}")

    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)
    monkeypatch.setattr(_plugin, "_STRIIM_DIR", tmp_path / "ctx")
    monkeypatch.setattr(_plugin, "_resolve_release", lambda config: {"STRIIM_VERSION": "5.4.0.6"})
    monkeypatch.setattr(_plugin, "_make_progress", lambda config: ((lambda *a, **k: None), (lambda: None)))
    return _plugin


class _Cluster:
    """Recorder for every lifecycle call and every provisioning-lock acquisition."""

    def __init__(self, monkeypatch, plugin, tmp_path, *, running, probes, reason, port_open=False):
        self.events = []
        self.up = False
        answers = iter(probes)

        def probe(*a, **k):
            if self.up:
                return True
            return next(answers, False)

        monkeypatch.setattr(plugin, "probe_reachable", probe)
        monkeypatch.setattr(plugin, "_striim5_running", lambda: running)
        monkeypatch.setattr(plugin, "_running_striim_version", lambda: "5.4.0.5")
        monkeypatch.setattr(plugin, "_redeploy_reason", lambda *a: reason)
        monkeypatch.setattr(sp, "ensure_deps", lambda *a, **k: self.events.append("deps"))
        monkeypatch.setattr(sp, "ensure_image", lambda *a, **k: self.events.append("image"))
        monkeypatch.setattr(sp, "wait_cluster_ready", lambda *a, **k: None)

        def down(*a, **k):
            self.events.append("down")

        def up(*a, **k):
            self.events.append("up")
            self.up = True

        monkeypatch.setattr(sp, "cluster_down", down)
        monkeypatch.setattr(sp, "cluster_up", up)
        real_lock = plugin.FileLock

        def lock(path, *a, **k):
            self.events.append("lock")
            return real_lock(str(tmp_path / "provision.lock"))

        monkeypatch.setattr(plugin, "FileLock", lock)

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def connect(addr, timeout=None):
            if port_open:
                return _Conn()
            raise OSError("closed")

        monkeypatch.setattr(socket, "create_connection", connect)
        monkeypatch.setattr(plugin, "StriimClient",
                            SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(list_deployment_groups=lambda: "")))
        monkeypatch.setattr(plugin, "parse_deployment_groups", lambda raw: "topology")
        monkeypatch.setenv("SLT_CLUSTER_SETTLE", "0")


WARM = [True]                           # the first probe answers: a reachable cluster
BOOTING = [False, False, False, True]   # three failed probes, then the boot wait succeeds


def _declared(mode, tmp_path):
    return SimpleNamespace(_slt_infra=infra.Infra(mode, 9080, tmp_path / "locks"))


def test_shared_identity_mismatch_reachable_branch_never_calls_cluster_down(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=WARM, reason="running 5.4.0.5, want 5.4.0.6")
    config = _declared("shared", tmp_path)
    assert plugin._resolve_striim(config) is None
    assert "shared cluster identity mismatch: running 5.4.0.5, want 5.4.0.6" in config._slt_striim_reason
    assert "never redeploys" in config._slt_striim_reason
    assert "down" not in c.events and "up" not in c.events and c.events == ["lock"]
    assert config._slt_infra.striim["status"] == "refused-identity-mismatch"


def test_shared_identity_mismatch_booting_branch_never_calls_cluster_down(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=BOOTING, reason="running 5.4.0.5, want 5.4.0.6")
    config = _declared("shared", tmp_path)
    assert plugin._resolve_striim(config) is None
    assert "shared cluster identity mismatch: running 5.4.0.5, want 5.4.0.6" in config._slt_striim_reason
    assert "never redeploys" in config._slt_striim_reason
    assert "down" not in c.events and "up" not in c.events and c.events == ["lock"]
    assert config._slt_infra.striim["status"] == "refused-identity-mismatch"


def test_shared_matching_booting_cluster_is_reused(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=BOOTING, reason=None)
    config = _declared("shared", tmp_path)
    assert plugin._resolve_striim(config) is not None
    assert c.events == ["lock"] and config._slt_striim_provisioned is False


def test_undeclared_booting_cluster_is_unchanged(plugin, monkeypatch, tmp_path):
    # a direct in-process caller without a declaration keeps today's behaviour: the booted cluster is used
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=BOOTING, reason="running 5.4.0.5, want 5.4.0.6")
    config = SimpleNamespace()
    assert plugin._resolve_striim(config) is not None
    assert "down" not in c.events and "up" not in c.events


def test_exclusive_refuses_reachable_endpoint_before_lock(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=WARM, reason="running 5.4.0.5, want 5.4.0.6")
    config = _declared("exclusive", tmp_path)
    assert plugin._resolve_striim(config) is None
    assert "already reachable before provisioning" in config._slt_striim_reason
    assert c.events == []                                  # no lock, no cluster_down, no cluster_up


def test_exclusive_refuses_open_port_boot_wait(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=[False] * 10,
                 reason="running 5.4.0.5, want 5.4.0.6", port_open=True)
    config = _declared("exclusive", tmp_path)
    assert plugin._resolve_striim(config) is None
    assert "already open before provisioning" in config._slt_striim_reason
    assert c.events == []


def test_shared_reuses_matching_cluster_without_provisioning(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=True, probes=WARM, reason=None)
    config = _declared("shared", tmp_path)
    ctx = plugin._resolve_striim(config)
    assert ctx is not None and ctx.mode == "docker"
    assert c.events == ["lock"]
    assert config._slt_striim_provisioned is False
    assert config._slt_infra.striim["status"] == "reused"


def test_shared_absent_cluster_provisioned_and_kept(plugin, monkeypatch, tmp_path):
    c = _Cluster(monkeypatch, plugin, tmp_path, running=False, probes=[False] * 3, reason=None)
    config = _declared("shared", tmp_path)
    ctx = plugin._resolve_striim(config)
    assert ctx is not None
    assert c.events == ["lock", "deps", "image", "up"] and "down" not in c.events
    assert config._slt_striim_provisioned is True
    assert config._slt_infra.striim["status"] == "provisioned-and-kept"


# ---------------------------------------------------------------- code review r1: R7, exclusive xdist members

LIVE = Path(__file__).resolve().parents[2]

_WORKER = r"""
import json, os, socket, sys, time, pathlib
from types import SimpleNamespace
work, role = pathlib.Path(sys.argv[1]), sys.argv[2]
up = work / "cluster-up"
real_sleep = time.sleep
from livetest import infra, plugin, stack
from livetest import striim_provision as sp

def log(event):
    with open(work / "events.log", "a") as f:
        f.write(f"{role} {event}\n")

def docker(argv):                                   # external infrastructure only
    if argv[:2] == ["docker", "ps"]:
        return SimpleNamespace(stdout="", returncode=0)
    if argv[:2] == ["docker", "inspect"]:
        bound = up.exists() and argv[-1] == stack.striim_container()
        ports = {"9080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9080"}]} if bound else {}
        return SimpleNamespace(stdout=json.dumps(ports), returncode=0)
    raise AssertionError(argv)

class _Conn:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

def connect(addr, timeout=None):
    if up.exists():
        return _Conn()
    raise OSError("closed")

def cluster_up(*a, **k):
    log("cluster_up")
    real_sleep(0.3)
    up.write_text("up")

infra._docker_ps = docker
socket.create_connection = connect
time.sleep = lambda s: None
plugin.probe_reachable = lambda *a, **k: up.exists()
plugin._striim5_running = lambda: up.exists()
plugin._running_striim_version = lambda: "5.4.0.6"
plugin._redeploy_reason = lambda *a: None
plugin._resolve_release = lambda config: {"STRIIM_VERSION": "5.4.0.6"}
plugin._STRIIM_DIR = work / "ctx"
plugin._make_progress = lambda config: ((lambda *a, **k: None), (lambda: None))
plugin._cluster_provision_lock = lambda: work / "provision.lock"
plugin.StriimClient = SimpleNamespace(from_url=lambda *a, **k: SimpleNamespace(list_deployment_groups=lambda: ""))
plugin.parse_deployment_groups = lambda raw: "topology"
sp.ensure_deps = sp.ensure_image = sp.wait_cluster_ready = lambda *a, **k: None
sp.cluster_up = cluster_up
sp.cluster_down = lambda *a, **k: log("cluster_down")
os.environ["SLT_CLUSTER_SETTLE"] = "0"

decl = infra.declare(os.environ, run=docker)
log("declared " + decl.lease_kind)
(work / f"declared-{role}").write_text("")
if role == "b":                                     # delayed: the run's cluster is already up
    while not (work / "a-resolved").exists():
        real_sleep(0.02)
config = SimpleNamespace(_slt_infra=decl)
ctx = plugin._resolve_striim(config)
print(json.dumps({"ok": ctx is not None, "reason": getattr(config, "_slt_striim_reason", None),
                  "provisioned": getattr(config, "_slt_striim_provisioned", None), "striim": decl.striim}), flush=True)
(work / f"{role}-resolved").write_text("")
if role == "b":                                     # outlive the holder, which finished early
    while not (work / "a-exited").exists():
        real_sleep(0.02)
    (work / "b-alive").write_text("")
    while not (work / "c-checked").exists():
        real_sleep(0.02)
if role == "a":                                     # keep the lease until b has declared, so b joins it: had a
    while not (work / "declared-b").exists():       # released first, b would hold the lease anew (same-run takeover)
        real_sleep(0.02)
decl.release()
log("released")
"""


def test_exclusive_joining_worker_binds_to_its_runs_cluster_and_the_run_outlives_an_early_holder(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "state").mkdir()   # a set SLT_STATE_DIR must exist (paths.state_dir, at plugin import)
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("SLT_") or k in ("PYTEST_XDIST_WORKER", "STRIIM_URL"))}
    env.update(PYTHONPATH=os.pathsep.join([str(LIVE), os.environ.get("PYTHONPATH", "")]), PYTHONDONTWRITEBYTECODE="1",
               SLT_LOCK_DIR=str(tmp_path / "locks"), SLT_STATE_DIR=str(tmp_path / "state"),
               SLT_INFRA_OWNERSHIP="exclusive", SLT_RUN_EPOCH="run-ab")

    def spawn(role):
        return subprocess.Popen([sys.executable, "-c", _WORKER, str(work), role], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_for(name, *procs):
        deadline = time.monotonic() + 60
        while not (work / name).exists():
            assert time.monotonic() < deadline and all(p.poll() is None for p in procs), \
                [p.communicate() for p in procs if p.poll() is not None]
            time.sleep(0.02)

    a = spawn("a")
    wait_for("declared-a", a)
    b = spawn("b")
    out_a, err_a = a.communicate(timeout=90)
    assert a.returncode == 0, err_a
    (work / "a-exited").write_text("")
    wait_for("b-alive", b)
    try:
        with pytest.raises(infra.InfraOwnershipError, match="still active"):
            infra.declare({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": "run-c"},
                          run=lambda argv: SimpleNamespace(stdout="", returncode=0))
    finally:
        (work / "c-checked").write_text("")
    out_b, err_b = b.communicate(timeout=90)
    assert b.returncode == 0, err_b
    ra, rb = json.loads(out_a.splitlines()[-1]), json.loads(out_b.splitlines()[-1])
    assert ra["ok"] and ra["provisioned"] is True and ra["striim"]["status"] == "owned", ra
    assert rb["ok"], rb["reason"]
    assert rb["provisioned"] is False and rb["striim"]["boundToAllocated"] is True and rb["striim"]["provisionedBy"] == "same-run"
    events = (work / "events.log").read_text().splitlines()
    assert "a declared held" in events and "b declared joined" in events
    assert [e for e in events if e.endswith("cluster_up")] == ["a cluster_up"] and not [e for e in events if "cluster_down" in e]
    after = infra.declare({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": "run-c"},
                          run=lambda argv: SimpleNamespace(stdout="", returncode=0))
    assert after.lease_kind == "held"                    # free once the run's last member released
    after.release()
