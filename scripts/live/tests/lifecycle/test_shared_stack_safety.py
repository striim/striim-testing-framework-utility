"""Shared-stack safety.

A stop is refused while another live session uses the stack, naming the pids and the `--force` command;
the console's app-node restart is refused on a shared cluster other sessions use; the pre-flight entry
points release their markers, and a marker of a dead or reused pid is pruned; the shared adapter brings a
dead container with a provision record up again and, under SLT_SHARED_PROVISION=0, refuses to start a
missing service; one shared provisioning lock per container.

Docker is always faked. Another session is a real child process, so its pid and start time are real.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace as NS

import psutil
import pytest
from filelock import FileLock

from livetest import cli, infra, preflight, services

PORT = 9080
SHARED = {"SLT_INFRA_OWNERSHIP": "shared", "SLT_KEEP_SERVICES": "1"}


@pytest.fixture
def other():
    """The pid of a live process standing for another session."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


def _shared_dir():
    return infra._shared_dir(infra._lock_dir(), PORT)


def _marker(pid, text=None):
    """A shared marker as a session of this framework writes it, or with ``text`` as its content."""
    d = _shared_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / str(pid)
    path.write_text(text if text is not None else
                    json.dumps({"pid": pid, "runId": "", "started": psutil.Process(pid).create_time()}))
    return path


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _logs(monkeypatch) -> list:
    logs = []
    monkeypatch.setattr(preflight, "_log", logs.append)
    return logs


def _fake_service_down(monkeypatch) -> list:
    downs = []
    monkeypatch.setattr(preflight, "compose_down", lambda defn: downs.append(defn.name))
    monkeypatch.setattr(preflight, "_surviving_containers", lambda defn: [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda *a, **k: None)
    return downs


def _fake_cluster(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {"STRIIM_VERSION": "5.4.2"})
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, r=None: True)
    monkeypatch.setattr(preflight._sp, "cluster_down", lambda d, r=None: calls.append("down -v"))
    return calls


# ------------------------------------------------------------------ stop with other live sessions

@pytest.mark.parametrize("env", [SHARED, {}], ids=["shared", "undeclared"])
def test_stop_of_a_service_is_refused_while_another_session_uses_the_stack(monkeypatch, other, env):
    _marker(other)
    downs, logs = _fake_service_down(monkeypatch), _logs(monkeypatch)
    assert preflight.teardown_services(["postgres"], dict(env)) == 2
    assert downs == []                                         # no `down -v`
    assert str(other) in logs[-1]
    assert "python -m livetest.cli stop postgres --force" in logs[-1]
    assert preflight.teardown_services(["postgres"], dict(env), force=True) == 0
    assert downs == ["postgres"]


def test_stop_of_the_cluster_is_refused_while_another_session_uses_it(monkeypatch, other):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    _marker(other)
    calls, logs = _fake_cluster(monkeypatch), _logs(monkeypatch)
    assert preflight.teardown_cluster() == 2
    assert calls == []
    assert str(other) in logs[-1] and "python -m livetest.cli stop striim --force" in logs[-1]
    assert preflight.teardown_cluster(force=True) == 0
    assert calls == ["down -v"]


def test_an_explicit_stop_of_a_test_id_is_refused_too(monkeypatch, other):
    _marker(other)
    logs, taken = _logs(monkeypatch), []
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [])
    monkeypatch.setattr(preflight, "compute_unions", lambda ms: (["postgres", "oracle"], []))
    monkeypatch.setattr(preflight, "_take_down_services",
                        lambda svcs, env, apply_gate=True: taken.extend(svcs) or [])
    assert preflight.teardown(["some-test"], dict(SHARED), explicit=True) == 2
    assert taken == []
    assert str(other) in logs[-1] and "python -m livetest.cli stop some-test --force" in logs[-1]
    assert preflight.teardown(["some-test"], dict(SHARED), explicit=True, force=True) == 0
    assert taken == ["postgres", "oracle"]


def test_with_no_other_live_session_a_stop_proceeds_and_dead_markers_are_pruned_first(monkeypatch):
    dead = _marker(_dead_pid(), "{}")                          # an old-format marker of a finished session
    own = _marker(os.getpid())                                 # this process is not "another session"
    downs = _fake_service_down(monkeypatch)
    assert preflight.teardown_services(["postgres"], dict(SHARED)) == 0
    assert downs == ["postgres"]
    assert not dead.exists() and own.exists()


def test_a_live_exclusive_run_refuses_a_stop_and_its_leftover_does_not(monkeypatch, other):
    lease = infra._exclusive_lease_path(infra._lock_dir(), PORT)
    owner = lease.parent / (lease.name + ".owner")
    owner.write_text(json.dumps({"runId": "run-x", "pid": other}))
    downs, logs = _fake_service_down(monkeypatch), _logs(monkeypatch)
    assert preflight.teardown_services(["postgres"], {"SLT_INFRA_OWNERSHIP": "exclusive"}) == 2
    assert "exclusive run run-x" in logs[-1] and str(other) in logs[-1]
    # the run's own stop (same run id) is not refused by itself
    assert preflight.teardown_services(["postgres"], {"SLT_RUN_EPOCH": "run-x"}) == 0
    owner.write_text(json.dumps({"runId": "run-x", "pid": _dead_pid()}))     # a leftover: nothing alive
    assert preflight.teardown_services(["postgres"], {"SLT_INFRA_OWNERSHIP": "exclusive"}) == 0
    assert downs == ["postgres", "postgres"]


def test_cli_stop_refuses_once_with_the_whole_command_and_force_skips_the_check(monkeypatch, other, capsys):
    _marker(other)
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    calls = []
    monkeypatch.setattr(cli, "preflight_teardown_services", lambda names, **kw: calls.append(("svc", kw)) or 0)
    monkeypatch.setattr(cli, "preflight_teardown_cluster", lambda **kw: calls.append(("cluster", kw)) or 0)
    assert cli.cmd_down(["postgres", "striim"]) == 2
    out = capsys.readouterr().out
    assert calls == []
    assert str(other) in out and "python -m livetest.cli stop postgres striim --force" in out
    assert cli.cmd_down(["postgres", "striim"], force=True) == 0
    assert calls == [("svc", {"force": True}), ("cluster", {"force": True})]


def test_cli_stop_accepts_force(monkeypatch):
    from livetest import project
    seen = {}
    monkeypatch.setattr(project, "load_and_activate", lambda: None)
    monkeypatch.setattr(cli, "_adopt_stack_prefix", lambda env=None: None)
    monkeypatch.setattr(cli, "cmd_down", lambda targets, force=False: seen.update(t=targets, f=force) or 0)
    monkeypatch.setattr(sys, "argv", ["livetest.cli", "stop", "postgres", "striim", "--force"])
    assert cli.main() == 0
    assert seen == {"t": ["postgres", "striim"], "f": True}


# ------------------------------------------------------------------ no session joins while a stop runs

_LIVE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DECLARE = """
import sys
from livetest import infra
try:
    decl = infra.declare({"SLT_INFRA_OWNERSHIP": sys.argv[1], "SLT_KEEP_SERVICES": "1"},
                         run=lambda argv: type("R", (), {"stdout": "", "returncode": 0})())
except infra.InfraOwnershipError as e:
    print("refused:", e)
else:
    decl.release()
    print("admitted")
"""


def _declare_elsewhere(mode="shared") -> str:
    """Another process declares on the endpoint now (a session starting while this one stops the stack)."""
    env = dict(os.environ, PYTHONPATH=_LIVE_DIR)
    out = subprocess.run([sys.executable, "-c", _DECLARE, mode], env=env, capture_output=True, text=True,
                         timeout=120)
    return (out.stdout + out.stderr).strip()


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
def test_no_session_is_admitted_while_a_stop_removes_the_stack(monkeypatch, mode):
    seen = []
    monkeypatch.setattr(preflight, "compose_down", lambda defn: seen.append(_declare_elsewhere(mode)))
    monkeypatch.setattr(preflight, "_surviving_containers", lambda defn: [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda *a, **k: None)
    assert preflight.teardown_services(["postgres"], dict(SHARED)) == 0
    assert len(seen) == 1 and seen[0].startswith("refused:"), seen
    assert "stop or restart" in seen[0] and str(os.getpid()) in seen[0]
    assert _declare_elsewhere(mode) == "admitted"             # the stop finished: sessions start again


def test_no_session_is_admitted_while_the_cluster_is_stopped(monkeypatch):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    seen = []
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {"STRIIM_VERSION": "5.4.2"})
    monkeypatch.setattr(preflight._sp, "cluster_has_containers", lambda d, r=None: True)
    monkeypatch.setattr(preflight._sp, "cluster_down", lambda d, r=None: seen.append(_declare_elsewhere()))
    assert preflight.teardown_cluster() == 0
    assert seen[0].startswith("refused:"), seen


def test_a_forced_stop_also_keeps_new_sessions_out(monkeypatch, other):
    _marker(other)
    seen = []
    monkeypatch.setattr(preflight, "compose_down", lambda defn: seen.append(_declare_elsewhere()))
    monkeypatch.setattr(preflight, "_surviving_containers", lambda defn: [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda *a, **k: None)
    assert preflight.teardown_services(["postgres"], dict(SHARED), force=True) == 0
    assert seen[0].startswith("refused:"), seen


def test_cli_stop_keeps_new_sessions_out_between_its_halves(monkeypatch):
    monkeypatch.setattr(cli, "all_services", lambda: ["postgres"])
    monkeypatch.setattr(cli, "known_test_ids", set)
    seen = []
    monkeypatch.setattr(preflight, "compose_down", lambda defn: None)
    monkeypatch.setattr(preflight, "_surviving_containers", lambda defn: [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda *a, **k: None)
    real = cli.preflight_teardown_services

    def services_half(names, **kw):
        rc = real(names, **kw)
        seen.append(_declare_elsewhere())                      # after the services, before the cluster
        return rc
    monkeypatch.setattr(cli, "preflight_teardown_services", services_half)
    monkeypatch.setattr(cli, "preflight_teardown_cluster", lambda **kw: 0)
    assert cli.cmd_down(["postgres", "striim"]) == 0
    assert seen[0].startswith("refused:"), seen
    assert _declare_elsewhere() == "admitted"


@pytest.mark.parametrize("error", [RuntimeError("compose hung"), KeyboardInterrupt()])
def test_a_stop_that_fails_or_is_interrupted_lets_sessions_start_again(monkeypatch, error):
    def down(defn):
        raise error
    monkeypatch.setattr(preflight, "compose_down", down)
    monkeypatch.setattr(preflight, "_surviving_containers", lambda defn: [])
    monkeypatch.setattr(preflight, "forget_provisioned", lambda *a, **k: None)
    try:
        preflight.teardown_services(["postgres"], dict(SHARED))
    except KeyboardInterrupt:
        pass
    assert _declare_elsewhere() == "admitted"


def test_the_record_of_a_stop_whose_process_died_does_not_keep_sessions_out():
    d = infra._stopping_dir(infra._lock_dir(), PORT)
    d.mkdir(parents=True, exist_ok=True)
    dead = d / str(_dead_pid())
    dead.write_text("{}")
    assert _declare_elsewhere() == "admitted"
    assert not dead.exists()


# ------------------------------------------------------------------ restart on a shared cluster

def _fake_restart(monkeypatch) -> list:
    restarted = []
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: NS(url="http://localhost:9080", user="admin", password="x", mode="docker"))
    monkeypatch.setattr(preflight.StriimClient, "from_url", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(preflight._sp, "restart_app_nodes", lambda client: restarted.append("restart"))
    return restarted


def test_restart_app_nodes_is_refused_on_a_shared_cluster_other_sessions_use(monkeypatch, other):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    _marker(other)
    restarted, logs = _fake_restart(monkeypatch), _logs(monkeypatch)
    assert preflight.restart_app_nodes() == 2
    assert restarted == []
    assert str(other) in logs[-1]
    assert not (_shared_dir() / str(os.getpid())).exists()     # its own marker is released too


def test_restart_app_nodes_runs_on_a_shared_cluster_no_other_session_uses(monkeypatch):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    restarted = _fake_restart(monkeypatch)
    assert preflight.restart_app_nodes() == 0
    assert restarted == ["restart"]


def test_no_session_is_admitted_while_the_app_nodes_restart(monkeypatch):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    seen = []
    monkeypatch.setattr(preflight, "_resolve_striim",
                        lambda cfg: NS(url="http://localhost:9080", user="admin", password="x", mode="docker"))
    monkeypatch.setattr(preflight.StriimClient, "from_url", classmethod(lambda cls, *a, **k: object()))
    monkeypatch.setattr(preflight._sp, "restart_app_nodes", lambda client: seen.append(_declare_elsewhere()))
    assert preflight.restart_app_nodes() == 0
    assert seen[0].startswith("refused:") and "stop or restart" in seen[0], seen
    assert _declare_elsewhere() == "admitted"
    assert not (_shared_dir() / str(os.getpid())).exists()


# ------------------------------------------------------------------ markers released and pruned

def _stub_provision(monkeypatch, seen):
    def resolve_striim(cfg):
        seen.append((_shared_dir() / str(os.getpid())).exists())   # declared while it works
        return None
    monkeypatch.setattr(preflight, "_resolve_striim", resolve_striim)
    monkeypatch.setattr(preflight, "_resolve_release", lambda cfg: {})
    monkeypatch.setattr(preflight, "manifests_for", lambda ids: [NS(requires=[], topology="single")])
    monkeypatch.setattr(preflight, "compute_unions", lambda ms: ([], []))
    monkeypatch.setattr(preflight, "clear_provision_registry", lambda d: None)

    def bring_up(*a, **k):
        seen.append((_shared_dir() / str(os.getpid())).exists())
        return {}
    monkeypatch.setattr(preflight, "_bring_up_services", bring_up)
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)


@pytest.mark.parametrize("entry, call", [
    ("provision", lambda: preflight.provision(["t"])),
    ("provision_cluster", lambda: preflight.provision_cluster()),
    ("provision_services", lambda: preflight.provision_services(["postgres"])),
    ("restart_app_nodes", lambda: preflight.restart_app_nodes()),
])
def test_preflight_entry_points_release_their_declaration(monkeypatch, entry, call):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    seen = []
    _stub_provision(monkeypatch, seen)
    call()
    assert seen == [True]                                      # the marker existed while it worked
    assert not (_shared_dir() / str(os.getpid())).exists()     # and is gone once it returned


def test_provision_services_releases_its_declaration_when_the_bring_up_raises(monkeypatch):
    for k, v in SHARED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(preflight, "_prune_dead_records", lambda names: None)

    def boom(*a, **k):
        raise RuntimeError("docker is down")
    monkeypatch.setattr(preflight, "_bring_up_services", boom)
    assert preflight.provision_services(["postgres"]) == 1
    assert not (_shared_dir() / str(os.getpid())).exists()


def test_a_shared_marker_records_its_process_start_time():
    decl = infra.declare(dict(SHARED, SLT_RUN_EPOCH="r"))
    try:
        record = json.loads((_shared_dir() / str(os.getpid())).read_text())
        assert record["started"] == psutil.Process(os.getpid()).create_time()
    finally:
        decl.release()


def test_a_shared_declaration_prunes_dead_markers():
    dead = _marker(_dead_pid(), "{}")
    decl = infra.declare(dict(SHARED))
    decl.release()
    assert not dead.exists()


def test_a_reused_pid_is_not_a_live_session(monkeypatch, other):
    path = _marker(other, json.dumps({"pid": other, "started": psutil.Process(other).create_time() - 1000}))
    assert infra._live_shared_markers(infra._lock_dir(), PORT, infra._pid_alive) == []
    assert not path.exists()                                   # pruned under the registry lock
    _marker(other, json.dumps({"pid": other, "started": psutil.Process(other).create_time() - 1000}))
    decl = infra.declare({"SLT_INFRA_OWNERSHIP": "exclusive", "SLT_RUN_EPOCH": "r"},
                         run=lambda argv: NS(stdout="", returncode=0))
    decl.release()                                             # an exclusive run is no longer refused
    _marker(other, json.dumps({"pid": other, "started": psutil.Process(other).create_time() - 1000}))
    swept = []
    preflight._sweep_postgres_slots(NS(sweep_stale_replication_slots=lambda log: swept.append(1)),
                                    infra.Infra("shared", PORT, infra._lock_dir()))
    assert swept == [1]                                        # and the slot sweep is not skipped


def test_an_old_format_marker_older_than_its_pids_process_is_stale(other):
    """Rule for a marker without a start time: its writer was running when it was written, so a process that
    started after the marker's file time (a reused pid, or anything after a reboot) is not its writer."""
    path = _marker(other, "{}")
    began = psutil.Process(other).create_time()
    os.utime(path, (began - 3600, began - 3600))
    assert infra._live_shared_markers(infra._lock_dir(), PORT, infra._pid_alive) == []
    assert not path.exists()


def test_an_old_format_marker_of_a_still_running_writer_still_counts(monkeypatch, other):
    _marker(other, json.dumps({"pid": other, "runId": ""}))        # as b649f9f wrote it, while `other` ran
    assert infra._live_shared_markers(infra._lock_dir(), PORT, infra._pid_alive) == [other]
    downs, _logs_ = _fake_service_down(monkeypatch), _logs(monkeypatch)
    assert preflight.teardown_services(["postgres"], dict(SHARED)) == 2
    assert downs == []


# ------------------------------------------------------------------ the shared adapter

def test_a_dead_container_with_a_provision_record_is_brought_up_again(monkeypatch, tmp_path):
    monkeypatch.setenv("SLT_PARALLEL", "1")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    defn = services.load_service("postgres")
    reg, _ = services._provision_state_paths()
    reg.write_text(json.dumps({defn.container: "1"}))
    ups = []
    inf = infra.Infra("shared", PORT, infra._lock_dir())
    res = infra.resolve_service(
        inf, "postgres", dict(os.environ), set(),
        lambda n, e, s, progress=None: services.resolve(n, e, s, compose_up=lambda d: ups.append(d.name),
                                                        post_up=lambda d: None),
        container_running=lambda c: False)
    assert ups == ["postgres"]                                 # `compose up` ran
    assert inf.services == [{"name": "postgres", "container": defn.container, "status": "provisioned-and-kept"}]
    assert res.started is True
    assert json.loads(reg.read_text()) == {defn.container: "1"}


def _defn(container="slt-postgres"):
    return NS(container=container, live_override_env=None)


def test_shared_provision_0_refuses_a_missing_service_and_names_the_start_command():
    inf = infra.Infra("shared", PORT, infra._lock_dir())
    with pytest.raises(infra.InfraOwnershipError,
                       match=r"SLT_SHARED_PROVISION=0.*start it with: SLT_SHARED_PROVISION=1 SLT_STACK_PREFIX=alt "
                             r"python -m livetest\.cli start postgres$"):
        infra.resolve_service(inf, "postgres", {"SLT_SHARED_PROVISION": "0", "SLT_STACK_PREFIX": "alt"}, set(),
                              lambda *a, **k: pytest.fail("a missing service was started"),
                              load=lambda n: _defn("alt-slt-postgres"), container_running=lambda c: False,
                              forget=lambda *a, **k: pytest.fail("nothing is forgotten"))
    assert inf.services == []
    started = set()
    assert infra.resolve_service(inf, "postgres", {"SLT_SHARED_PROVISION": "0"}, started, lambda *a, **k: "r",
                                 load=lambda n: _defn(), container_running=lambda c: True) == "r"
    assert inf.services == [{"name": "postgres", "container": "slt-postgres", "status": "reused"}]


def test_the_start_command_a_shared_provision_0_refusal_names_starts_the_service(monkeypatch, tmp_path):
    """Follow the printed recovery from the lane's own environment, as an operator pasting it into the same
    shell would: it must start the service, not meet the same refusal. Only the service start is stubbed."""
    import shlex
    from livetest import project
    (tmp_path / "state").mkdir()
    lane = dict(SHARED, SLT_SHARED_PROVISION="0", SLT_STACK_PREFIX="alt", SLT_STATE_DIR=str(tmp_path / "state"))
    for k, v in lane.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(services, "container_running", lambda c, run=None: False)     # kafka is not running
    monkeypatch.setattr(preflight, "container_running", lambda c, run=None: False)
    with pytest.raises(infra.InfraOwnershipError) as ei:            # the run's refusal
        infra.resolve_service(infra.Infra("shared", PORT, infra._lock_dir()), "kafka", dict(os.environ), set(),
                              lambda *a, **k: pytest.fail("a missing service was started"))
    words = shlex.split(str(ei.value).split("start it with: ", 1)[1])
    assignments = {}
    while words and "=" in words[0] and not words[0].startswith("-"):
        k, v = words.pop(0).split("=", 1)
        assignments[k] = v
    assert words[:3] == ["python", "-m", "livetest.cli"], words
    for k, v in assignments.items():                                # `VAR=value command` in that shell
        monkeypatch.setenv(k, v)
    started = []
    monkeypatch.setattr(preflight, "resolve", lambda n, e, s, progress=None: started.append(n) or n)
    monkeypatch.setattr(project, "load_and_activate", lambda: None)
    monkeypatch.setattr(cli, "known_test_ids", set)
    monkeypatch.setattr(sys, "argv", ["livetest.cli", *words[3:]])
    assert cli.main() == 0
    assert started == ["kafka"]
    assert os.environ["SLT_STACK_PREFIX"] == "alt"                  # the same stack


@pytest.mark.parametrize("env", [{"SLT_PARALLEL": "1"}, {"SLT_PARALLEL": "1", "SLT_SHARED_PROVISION": "1"}])
def test_by_default_a_shared_run_starts_a_missing_service(env):
    inf, forgotten, ran = infra.Infra("shared", PORT, infra._lock_dir()), [], []
    infra.resolve_service(inf, "postgres", env, set(), lambda *a, **k: ran.append(1),
                          load=lambda n: _defn(), container_running=lambda c: False,
                          forget=lambda keys, timeout=-1: forgotten.append(keys))
    assert ran == [1] and forgotten == [["slt-postgres", "postgres-setup"]]
    assert inf.services[0]["status"] == "provisioned-and-kept"


def test_a_serial_shared_run_does_not_touch_the_provision_registry():
    inf, ran = infra.Infra("shared", PORT, infra._lock_dir()), []
    infra.resolve_service(inf, "postgres", {}, set(), lambda *a, **k: ran.append(1), load=lambda n: _defn(),
                          container_running=lambda c: False,
                          forget=lambda *a, **k: pytest.fail("a serial run reads no provision registry"))
    assert ran == [1]


@pytest.mark.parametrize("value", ["yes", " 1", "true"])
def test_shared_provision_takes_only_1_or_0(value):
    # A switch like the others: the strict helper refuses anything but 1 or 0 (unset is on).
    with pytest.raises(infra.InfraOwnershipError, match=rf"SLT_SHARED_PROVISION={value!r}: a switch is 1"):
        infra.resolve_service(infra.Infra("shared", PORT, infra._lock_dir()), "postgres",
                              {"SLT_SHARED_PROVISION": value}, set(), lambda *a, **k: pytest.fail("resolved"),
                              load=lambda n: _defn(), container_running=lambda c: False)


# ------------------------------------------------------------------ one lock per service container

def test_one_services_provisioning_lock_does_not_block_another():
    held = FileLock(str(infra._lock_dir() / infra.shared_provision_lock("slt-oracle")))
    held.acquire()                                             # another lane cold-starting oracle
    try:
        inf = infra.Infra("shared", PORT, infra._lock_dir())
        assert infra.resolve_service(inf, "postgres", {}, set(), lambda *a, **k: "r", load=lambda n: _defn(),
                                     container_running=lambda c: True, lock_timeout=0.2) == "r"
        oracle = infra.Infra("shared", PORT, infra._lock_dir())
        t0 = time.monotonic()
        with pytest.raises(infra.InfraOwnershipError, match="lock-timeout"):
            infra.resolve_service(oracle, "oracle", {}, set(), lambda *a, **k: pytest.fail("resolved"),
                                  load=lambda n: _defn("slt-oracle"), container_running=lambda c: True,
                                  lock_timeout=0.2)
        assert time.monotonic() - t0 < 10
    finally:
        held.release()


_COLD_ORACLE = """
import os, sys, time
from pathlib import Path
from livetest import infra, services
began, release = Path(sys.argv[1]), Path(sys.argv[2])

def up(defn):                                   # `docker compose up --wait` of a cold Oracle
    began.write_text(defn.container)
    deadline = time.monotonic() + 60
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

inf = infra.Infra("shared", 9080, infra._lock_dir())
infra.resolve_service(inf, "oracle", dict(os.environ), set(),
                      lambda n, e, s, progress=None: services.resolve(n, e, s, compose_up=up,
                                                                      post_up=lambda d: None),
                      container_running=lambda c: False)
print(inf.services[0]["status"])
"""


def test_a_cold_oracle_start_does_not_hold_up_a_missing_postgres(monkeypatch, tmp_path):
    """Two shared parallel workers, both services missing, through the real resolver and the real provision
    registry; only Docker (`compose up`) and post-up are stubbed. Postgres is provisioned while another
    process is still inside Oracle's bring-up."""
    import threading
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(state))
    monkeypatch.setenv("SLT_PARALLEL", "1")
    began, release = tmp_path / "oracle-began", tmp_path / "oracle-release"
    oracle = subprocess.Popen([sys.executable, "-c", _COLD_ORACLE, str(began), str(release)],
                              env=dict(os.environ, PYTHONPATH=_LIVE_DIR), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    ups, done = [], threading.Event()

    def postgres_worker():
        inf = infra.Infra("shared", PORT, infra._lock_dir())
        infra.resolve_service(inf, "postgres", dict(os.environ), set(),
                              lambda n, e, s, progress=None: services.resolve(
                                  n, e, s, compose_up=lambda d: ups.append(d.name), post_up=lambda d: None),
                              container_running=lambda c: False)
        done.set()

    worker = threading.Thread(target=postgres_worker, daemon=True)
    try:
        deadline = time.monotonic() + 60
        while not began.exists() and oracle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert began.exists(), oracle.communicate()[0]          # Oracle's bring-up is under way
        worker.start()
        assert done.wait(15), "postgres waited behind the oracle bring-up"
        assert ups == ["postgres"] and oracle.poll() is None    # while oracle is still coming up
    finally:
        release.touch()
        out = oracle.communicate(timeout=60)[0]
        worker.join(60)
    assert oracle.returncode == 0 and out.strip().endswith("provisioned-and-kept"), out
    registry = json.loads(services._provision_state_paths()[0].read_text())
    assert {services.load_service("oracle").container, services.load_service("postgres").container} <= set(registry)


def test_a_service_whose_container_is_named_registry_is_provisioned(monkeypatch, tmp_path):
    """A valid consumer container name that equals a name the provisioning code uses for its own files:
    its provisioning lock must not be the registry's lock, which the same bring-up takes inside it. Through
    the real resolver and the real provision registry; only Docker (`compose up`) and post-up are stubbed,
    and the registry lock's wait is cut to 2 s so a collision fails quickly."""
    import threading
    from livetest import layout
    svc = tmp_path / "services" / "regsvc"
    svc.mkdir(parents=True)
    (svc / "service.yaml").write_text("name: regsvc\ncompose: compose.yaml\ncontainer: registry\nisolation: none\n")
    (svc / "compose.yaml").write_text("services: {}\n")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SLT_STATE_DIR", str(state))
    monkeypatch.setenv("SLT_PARALLEL", "1")
    monkeypatch.setattr(services, "_REGISTRY_WAIT_S", 2.0)
    layout.set_roots(services=[tmp_path / "services"])
    ups, box = [], {}

    def bring_up():
        inf = infra.Infra("shared", PORT, infra._lock_dir())
        try:
            infra.resolve_service(inf, "regsvc", dict(os.environ), set(),
                                  lambda n, e, s, progress=None: services.resolve(
                                      n, e, s, compose_up=lambda d: ups.append(d.container), post_up=lambda d: None),
                                  container_running=lambda c: False)
            box["status"] = inf.services[0]["status"]
        except BaseException as e:      # noqa: BLE001 - handed back to the test
            box["error"] = e

    try:
        worker = threading.Thread(target=bring_up, daemon=True)
        worker.start()
        worker.join(30)
        assert not worker.is_alive(), "the bring-up never finished"
        assert "error" not in box, box["error"]
        assert ups == ["registry"] and box["status"] == "provisioned-and-kept"
        assert "registry" in json.loads(services._provision_state_paths()[0].read_text())
    finally:
        layout._reset()


@pytest.mark.parametrize("prefix", ["", "alt"])
@pytest.mark.parametrize("key", ["registry", "slt-registry", "key-registry", ".slt-provision-registry"])
def test_no_container_name_makes_a_provisioning_lock_another_lock_file(monkeypatch, prefix, key):
    monkeypatch.setenv("SLT_STACK_PREFIX", prefix)
    lock = services._provision_key_lock(key)
    assert lock != services._provision_state_paths()[1]                     # the registry's lock
    assert lock.name.startswith(f".{prefix}-slt-provision-key-" if prefix else ".slt-provision-key-")
