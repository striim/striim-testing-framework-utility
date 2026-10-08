"""Hermetic tests for the `service_outage` action: manifest parsing and the docker/status
dispatch in livetest.outage. No Docker: `run` and the client are stubs."""
from pathlib import Path
import subprocess
import textwrap

import pytest

from livetest.manifest import load_manifest, ManifestError
from livetest import outage
from livetest.outage import OutageError


def _write(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "so"
    d.mkdir()
    (d / "test.yaml").write_text(textwrap.dedent(body))
    return d / "test.yaml"


_BASE = """\
name: so
tql: app.tql
requires: [postgres]
assert:
  smoke: true
"""


def _load(tmp_path, action: str, base: str = _BASE):
    return load_manifest(_write(tmp_path, base + "action:\n" + action))


# --- manifest parsing -----------------------------------------------------------------

def test_defaults(tmp_path):
    m = _load(tmp_path, "  - type: service_outage\n    service: postgres\n")
    assert m.action_specs == [{
        "type": "service_outage", "service": "postgres", "cycles": 1, "delay_before": 3.0,
        "down_for": 5.0, "signal": "KILL", "ready_timeout": 120.0, "settle": 30.0,
        "concurrent": []}]


def test_all_fields(tmp_path):
    m = _load(tmp_path, (
        "  - type: service_outage\n    service: postgres\n    cycles: 2\n"
        "    delay_before: 0\n    down_for: 10s\n    signal: TERM\n"
        "    ready_timeout: 1m\n    settle: 0\n"))
    s = m.action_specs[0]
    assert (s["cycles"], s["delay_before"], s["down_for"], s["signal"],
            s["ready_timeout"], s["settle"]) == (2, 0.0, 10.0, "TERM", 60.0, 0.0)


@pytest.mark.parametrize("action,match", [
    ("    service: postgres\n    signal: HUP\n", "signal"),
    ("    signal: KILL\n", "non-empty 'service'"),
    ("    service: '  '\n", "non-empty 'service'"),
    ("    service: kafka\n", "must be listed in 'requires'"),
    ("    service: postgres\n    cycles: 0\n", "cycles"),
    ("    service: postgres\n    downfor: 5\n", "unknown key"),
])
def test_rejects(tmp_path, action, match):
    with pytest.raises(ManifestError, match=match):
        _load(tmp_path, "  - type: service_outage\n" + action)


def test_concurrent_parsed_identically_for_both_types(tmp_path):
    conc = ("    concurrent:\n      - db: postgres-source\n        file: loop.sql\n"
            "        loop_interval: 500ms\n")
    m = _load(tmp_path, ("  - type: stop_start_cycle\n" + conc
                         + "  - type: service_outage\n    service: postgres\n" + conc))
    assert m.action_specs[0]["concurrent"] == m.action_specs[1]["concurrent"] == [
        {"db": "postgres-source", "file": "loop.sql", "loop_interval": 0.5}]


@pytest.mark.parametrize("conc,match", [
    ("    concurrent: loop.sql\n", "'concurrent' must be a list"),
    ("    concurrent:\n      - loop.sql\n", "must be a dict"),
    ("    concurrent:\n      - file: loop.sql\n", "must have 'db' key"),
    ("    concurrent:\n      - db: postgres-source\n", "must have 'file' key"),
])
def test_concurrent_rejects(tmp_path, conc, match):
    with pytest.raises(ManifestError, match=match):
        _load(tmp_path, "  - type: service_outage\n    service: postgres\n" + conc)


# --- interrupt() ----------------------------------------------------------------------

class FakeRun:
    """Records argv; answers `inspect` from a queue of stdout values (last one repeats)."""
    def __init__(self, inspect=("false",), returncode=0):
        self.calls = []
        self._inspect = list(inspect)
        self._rc = returncode

    def __call__(self, argv):
        self.calls.append(list(argv))
        out = ""
        if argv[:3] == ["docker", "container", "inspect"]:
            out = self._inspect.pop(0) if len(self._inspect) > 1 else self._inspect[0]

        class R:
            returncode = self._rc
            stderr = "boom"
            stdout = out
        return R()


@pytest.mark.parametrize("signal,argv", [
    ("KILL", ["docker", "kill", "-s", "KILL", "c1"]),
    ("TERM", ["docker", "stop", "c1"]),
])
def test_interrupt_command(signal, argv):
    run = FakeRun(inspect=("true", "false"))
    outage.interrupt("c1", signal, run=run, poll=0)
    assert run.calls[0] == argv
    assert run.calls[1] == ["docker", "container", "inspect", "-f", "{{.State.Running}}", "c1"]
    assert len(run.calls) == 3


def test_interrupt_raises_when_still_running():
    with pytest.raises(OutageError, match="still running"):
        outage.interrupt("c1", "KILL", run=FakeRun(inspect=("true",)), verify_timeout=0, poll=0)


def test_interrupt_raises_on_docker_failure():
    with pytest.raises(OutageError, match="failed"):
        outage.interrupt("c1", "KILL", run=FakeRun(returncode=1), poll=0)


# --- restore() ------------------------------------------------------------------------

def test_restore_starts_and_polls_until_healthy():
    run = FakeRun(inspect=("starting|true", "healthy|true"))
    outage.restore("c1", run=run, poll=0)
    assert run.calls[0] == ["docker", "start", "c1"]
    assert len(run.calls) == 3


def test_restore_without_healthcheck_waits_fixed(monkeypatch):
    slept = []
    monkeypatch.setattr(outage.time, "sleep", slept.append)
    outage.restore("c1", run=FakeRun(inspect=("<no value>|true",)), poll=0)
    assert slept == [5.0]


def test_restore_raises_on_timeout_naming_last_status():
    with pytest.raises(OutageError, match="health=unhealthy running=true"):
        outage.restore("c1", run=FakeRun(inspect=("unhealthy|true",)), ready_timeout=0, poll=0)


def test_polls_report_progress():
    ticks = []
    outage.restore("c1", run=FakeRun(inspect=("starting|true", "healthy|true")), poll=0,
                   progress=lambda e, t: ticks.append(t))
    assert ticks == [120.0, 120.0]


# --- cycle() --------------------------------------------------------------------------

_START = ["docker", "start", "c1"]


def test_cycle_restores_after_the_wait():
    run = FakeRun(inspect=("false", "healthy|true"))
    waited = []
    outage.cycle("c1", "KILL", 5.0, 10.0, wait=waited.append, run=run)
    assert waited == [5.0] and run.calls.count(_START) == 1


def test_cycle_wait_raising_still_starts():
    run = FakeRun(inspect=("false", "healthy|true"))

    def wait(_s):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        outage.cycle("c1", "KILL", 5.0, 10.0, wait=wait, run=run)
    assert run.calls.count(_START) == 1


def test_cycle_interrupt_raising_still_starts(monkeypatch):
    real = outage.interrupt
    monkeypatch.setattr(outage, "interrupt",
                        lambda *a, **k: real(*a, **{**k, "verify_timeout": 0, "poll": 0}))
    run = FakeRun(inspect=("true", "healthy|true"))
    with pytest.raises(OutageError, match="still running"):
        outage.cycle("c1", "TERM", 5.0, 10.0, wait=lambda s: None, run=run)
    assert run.calls.count(_START) == 1


def test_cycle_restore_failure_does_not_mask_original():
    said = []
    run = FakeRun(inspect=("false", "unhealthy|true"))

    def wait(_s):
        raise RuntimeError("wait broke")
    with pytest.raises(RuntimeError, match="wait broke") as ei:
        outage.cycle("c1", "KILL", 5.0, 0.0, wait=wait, run=run, report=said.append)
    assert any("also failed" in n for n in ei.value.__notes__)
    assert any("also failed" in m for m in said)


def test_cycle_does_not_retry_a_failed_restore():
    run = FakeRun(inspect=("false", "unhealthy|true"))
    with pytest.raises(OutageError, match="not ready"):
        outage.cycle("c1", "KILL", 0.0, 0.0, wait=lambda s: None, run=run)
    assert run.calls.count(_START) == 1


# --- watch_app() ----------------------------------------------------------------------

class StubClient:
    def __init__(self, statuses):
        self._statuses = list(statuses)

    def current_status(self, app):
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]


def test_watch_app_halt_raises():
    with pytest.raises(OutageError, match="left RUNNING during the outage settle \\(now HALT\\)"):
        outage.watch_app(StubClient(["RUNNING", "HALT"]), "a", settle=5, poll=0)


def test_watch_app_running_returns():
    assert outage.watch_app(StubClient(["RUNNING"]), "a", settle=0.01, poll=0) == "RUNNING"


def test_watch_app_completed_breaks():
    c = StubClient(["COMPLETED", "HALT"])
    assert outage.watch_app(c, "a", settle=60, poll=0) == "COMPLETED"


# --- container_for() ------------------------------------------------------------------

def test_container_for_live_override_skips():
    c, why = outage.container_for("postgres", env={"SLT_PG_HOST": "db.example"})
    assert c is None and "SLT_PG_HOST" in why


def test_container_for_unknown_service():
    c, why = outage.container_for("nosuchservice", env={})
    assert c is None and "unknown service" in why


def test_container_for_docker_service():
    c, why = outage.container_for("postgres", env={})
    assert c and why == ""


# --- graceful_stop --------------------------------------------------------------------

_GRACE = ("kill -TERM $(pidof srv)", 60.0)
_EXEC = ["docker", "exec", "c1", "sh", "-c", "kill -TERM $(pidof srv)"]


def test_term_with_graceful_stop_execs_it_and_waits():
    run = FakeRun(inspect=("true", "false"))
    outage.interrupt("c1", "TERM", run=run, poll=0, graceful=_GRACE)
    assert run.calls[0] == _EXEC
    assert ["docker", "stop", "c1"] not in run.calls and len(run.calls) == 3


def test_kill_ignores_graceful_stop():
    run = FakeRun(inspect=("false",))
    outage.interrupt("c1", "KILL", run=run, poll=0, graceful=_GRACE)
    assert run.calls[0] == ["docker", "kill", "-s", "KILL", "c1"]


def test_graceful_stop_exec_failing_as_the_container_exits_is_the_stop():
    outage.interrupt("c1", "TERM", run=FakeRun(inspect=("false",), returncode=137), poll=0,
                     graceful=_GRACE)


def test_graceful_stop_exec_failing_on_a_running_container_raises(monkeypatch):
    monkeypatch.setattr(outage, "EXEC_EXIT_GRACE", 0.0)
    with pytest.raises(OutageError, match="exec c1 failed"):
        outage.interrupt("c1", "TERM", run=FakeRun(inspect=("true",), returncode=1), poll=0,
                         graceful=_GRACE)


def test_graceful_stop_exec_failing_while_the_container_goes_down_is_the_stop():
    run = FakeRun(inspect=("true", "false"), returncode=1)
    outage.interrupt("c1", "TERM", run=run, poll=0, graceful=_GRACE)
    assert run.calls[0] == _EXEC


class HangingExec(FakeRun):
    def __call__(self, argv):
        if argv[:2] == ["docker", "exec"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return super().__call__(argv)


def test_graceful_stop_exec_that_hangs_is_killed_and_raises():
    run = HangingExec(inspect=("true",))
    with pytest.raises(OutageError, match="did not return in 60s; KILLed"):
        outage.interrupt("c1", "TERM", run=run, poll=0, graceful=_GRACE)
    assert run.calls[-1] == ["docker", "kill", "-s", "KILL", "c1"]


def test_graceful_stop_kill_failure_is_named():
    with pytest.raises(OutageError, match="docker kill failed too: 'boom'"):
        outage.interrupt("c1", "TERM", run=HangingExec(inspect=("true",), returncode=1), poll=0,
                         graceful=_GRACE)


def test_graceful_stop_that_does_not_finish_is_killed_and_raises():
    run = FakeRun(inspect=("true",))
    with pytest.raises(OutageError, match="after its graceful_stop; KILLed"):
        outage.interrupt("c1", "TERM", run=run, poll=0, graceful=(_GRACE[0], 0.0))
    assert run.calls[-1] == ["docker", "kill", "-s", "KILL", "c1"]


def test_cycle_passes_graceful_stop_through():
    run = FakeRun(inspect=("false", "healthy|true"))
    outage.cycle("c1", "TERM", 0.0, 10.0, wait=lambda s: None, run=run, graceful=_GRACE)
    assert run.calls[0] == _EXEC and run.calls.count(_START) == 1


_SERVICES = Path(outage.__file__).resolve().parents[1] / "services"


def test_graceful_stop_for_mssql_and_none_for_postgres():
    cmd, timeout = outage.graceful_stop_for("mssql", env={}, services_dir=_SERVICES)
    assert "sqlservr" in cmd and timeout == 60.0
    assert outage.graceful_stop_for("postgres", env={}, services_dir=_SERVICES) == (None, 0.0)


def _svc(tmp_path, extra: str):
    d = tmp_path / "gsx"
    d.mkdir()
    (d / "service.yaml").write_text("name: gsx\nisolation: none\ncontainer: slt-gsx\n" + extra)
    return tmp_path


def test_graceful_stop_for_defaults_the_timeout(tmp_path):
    assert outage.graceful_stop_for("gsx", env={}, services_dir=_svc(
        tmp_path, "graceful_stop: kill -TERM 1\n")) == ("kill -TERM 1", 60.0)


@pytest.mark.parametrize("extra,match", [
    ("graceful_stop: ''\n", "graceful_stop"),
    ("graceful_stop: [kill]\n", "graceful_stop"),
    ("graceful_stop: x\ngraceful_stop_timeout: 0\n", "graceful_stop_timeout"),
    ("graceful_stop: x\ngraceful_stop_timeout: true\n", "graceful_stop_timeout"),
    ("graceful_stop: x\ngraceful_stop_timeout: .inf\n", "graceful_stop_timeout"),
    ("graceful_stop: x\ngraceful_stop_timeout: .nan\n", "graceful_stop_timeout"),
    ("graceful_stop_timeout: 30\n", "needs 'graceful_stop'"),
])
def test_graceful_stop_keys_are_validated(tmp_path, extra, match):
    from livetest import registry
    with pytest.raises(registry.RegistryError, match=match):
        registry.load_service("gsx", services_dir=_svc(tmp_path, extra))


def test_graceful_stop_keys_load(tmp_path):
    from livetest import registry
    d = registry.load_service("gsx", services_dir=_svc(
        tmp_path, "graceful_stop: kill -TERM 1\ngraceful_stop_timeout: 90\n"))
    assert (d.graceful_stop, d.graceful_stop_timeout) == ("kill -TERM 1", 90.0)


# --- restart_in_place (signal RESTART) ------------------------------------------------

_RIP = ("/restart-dbs.sh", 900.0)
_RIP_EXEC = ["docker", "exec", "c1", "sh", "-c", "/restart-dbs.sh"]


def _rip_service(monkeypatch, hook="/restart-dbs.sh"):
    from livetest import registry
    real = registry.load_service

    def load(name, *a, **kw):
        d = real(name, *a, **kw)
        d.restart_in_place = hook
        return d
    monkeypatch.setattr(registry, "load_service", load)


def test_restart_manifest(tmp_path, monkeypatch):
    _rip_service(monkeypatch)
    s = _load(tmp_path, "  - type: service_outage\n    service: postgres\n    signal: RESTART\n"
                        "    settle: 1m\n").action_specs[0]
    assert (s["signal"], s["down_for"], s["settle"]) == ("RESTART", 0.0, 60.0)


@pytest.mark.parametrize("key", ["down_for", "ready_timeout"])
def test_restart_manifest_refuses_stop_start_keys(tmp_path, monkeypatch, key):
    _rip_service(monkeypatch)
    with pytest.raises(ManifestError, match=f"'{key}' does not apply to signal RESTART"):
        _load(tmp_path, "  - type: service_outage\n    service: postgres\n    signal: RESTART\n"
                        f"    {key}: 5\n")


def test_restart_manifest_needs_the_hook(tmp_path):
    with pytest.raises(ManifestError, match="needs service 'postgres' to set 'restart_in_place'"):
        _load(tmp_path, "  - type: service_outage\n    service: postgres\n    signal: RESTART\n")


def test_restart_execs_the_hook_and_never_stops_or_starts():
    run = FakeRun()
    outage.cycle("c1", "RESTART", 0.0, 10.0, wait=lambda s: pytest.fail("waited"), run=run,
                 restart=_RIP)
    assert run.calls == [_RIP_EXEC]


def test_restart_without_a_hook_raises():
    run = FakeRun()
    with pytest.raises(OutageError, match="needs the service's restart_in_place"):
        outage.cycle("c1", "RESTART", 0.0, 10.0, wait=lambda s: None, run=run)
    assert run.calls == []


def test_restart_runs_the_exec_under_its_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(outage.subprocess, "run",
                        lambda a, **kw: seen.append((a, kw["timeout"])) or FakeRun()(a))
    outage.restart_in_place("c1", ("x", 42.0))
    assert seen == [(["docker", "exec", "c1", "sh", "-c", "x"], 42.0)]


def test_restart_hook_failing_raises_and_does_not_kill():
    run = FakeRun(returncode=3)
    with pytest.raises(OutageError, match=r"(?s)failed \(exit 3\).*left running, not KILLed.*output:\nboom"):
        outage.cycle("c1", "RESTART", 0.0, 10.0, wait=lambda s: None, run=run, restart=_RIP)
    assert run.calls == [_RIP_EXEC]


def test_restart_hook_that_hangs_raises_and_does_not_kill():
    run = HangingExec()
    with pytest.raises(OutageError, match=r"did not return in 900s \(HANG;.*not KILLed"):
        outage.cycle("c1", "RESTART", 0.0, 10.0, wait=lambda s: None, run=run, restart=_RIP)
    assert all(c[:2] == ["docker", "exec"] for c in run.calls)


def test_restart_reports_progress_while_the_hook_runs():
    import threading
    gate = threading.Event()
    ticks = []

    def run(argv):
        gate.wait(5)
        return FakeRun()(argv)
    t = threading.Timer(0.05, gate.set)
    t.start()
    outage.restart_in_place("c1", _RIP, run=run, poll=0.01,
                            progress=lambda e, total: ticks.append(total))
    assert ticks and set(ticks) == {900.0}


def test_restart_in_place_for_defaults_the_timeout(tmp_path):
    assert outage.restart_in_place_for("gsx", env={}, services_dir=_svc(
        tmp_path, "restart_in_place: /r.sh\n")) == ("/r.sh", 900.0)
    assert outage.restart_in_place_for("postgres", env={}, services_dir=_SERVICES) == (None, 0.0)


@pytest.mark.parametrize("extra,match", [
    ("restart_in_place: ''\n", "restart_in_place"),
    ("restart_in_place: [r]\n", "restart_in_place"),
    ("restart_in_place: x\nrestart_in_place_timeout: 0\n", "restart_in_place_timeout"),
    ("restart_in_place: x\nrestart_in_place_timeout: true\n", "restart_in_place_timeout"),
    ("restart_in_place: x\nrestart_in_place_timeout: .inf\n", "restart_in_place_timeout"),
    ("restart_in_place_timeout: 30\n", "needs 'restart_in_place'"),
])
def test_restart_in_place_keys_are_validated(tmp_path, extra, match):
    from livetest import registry
    with pytest.raises(registry.RegistryError, match=match):
        registry.load_service("gsx", services_dir=_svc(tmp_path, extra))


def test_restart_in_place_keys_load(tmp_path):
    from livetest import registry
    d = registry.load_service("gsx", services_dir=_svc(
        tmp_path, "restart_in_place: /r.sh\nrestart_in_place_timeout: 600\n"))
    assert (d.restart_in_place, d.restart_in_place_timeout) == ("/r.sh", 600.0)


def test_restart_exec_oserror_propagates():
    def run(argv):
        run.calls.append(list(argv))
        raise OSError("docker: not found")
    run.calls = []
    with pytest.raises(OSError, match="docker: not found"):
        outage.cycle("c1", "RESTART", 0.0, 10.0, wait=lambda s: None, run=run, restart=_RIP)
    assert run.calls == [_RIP_EXEC]


def test_restart_failure_names_stdout_when_stderr_is_empty():
    class R:
        returncode, stderr, stdout = 3, "", "restart not observed\n"
    with pytest.raises(OutageError, match=r"(?s)failed \(exit 3\).*output:\nrestart not observed$"):
        outage.restart_in_place("c1", _RIP, run=lambda a: R())


def test_restart_failure_keeps_only_the_last_lines():
    class R:
        returncode, stdout = 1, ""
        stderr = "\n".join(f"line {i}" for i in range(100))
    with pytest.raises(OutageError) as e:
        outage.restart_in_place("c1", _RIP, run=lambda a: R())
    assert "line 79" not in str(e.value)
    assert str(e.value).endswith("\n".join(f"line {i}" for i in range(80, 100)))


def test_restart_hang_keeps_the_output_so_far():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 900, output=b"DBS Startup - Starting AMP Partitions\n")
    with pytest.raises(OutageError, match=r"(?s)HANG.*output so far:\nDBS Startup - Starting AMP"):
        outage.restart_in_place("c1", _RIP, run=run)


_BOTH = "graceful_stop: kill -TERM 1\nrestart_in_place: /r.sh\n"


@pytest.mark.parametrize("signal,want", [
    ("KILL", (None, None)),
    ("TERM", (("kill -TERM 1", 60.0), None)),
    ("RESTART", (None, ("/r.sh", 900.0))),
])
def test_hooks_for_picks_the_signal_s_hook(tmp_path, signal, want):
    assert outage.hooks_for("gsx", signal, env={}, services_dir=_svc(tmp_path, _BOTH)) == want


@pytest.mark.parametrize("signal", ["KILL", "TERM"])
def test_hooks_for_looks_up_restart_in_place_only_for_restart(tmp_path, monkeypatch, signal):
    monkeypatch.setattr(outage, "restart_in_place_for",
                        lambda *a, **kw: pytest.fail("restart_in_place looked up"))
    outage.hooks_for("gsx", signal, env={}, services_dir=_svc(tmp_path, _BOTH))


@pytest.mark.parametrize("signal", ["KILL", "RESTART"])
def test_hooks_for_looks_up_graceful_stop_only_for_term(tmp_path, monkeypatch, signal):
    monkeypatch.setattr(outage, "graceful_stop_for",
                        lambda *a, **kw: pytest.fail("graceful_stop looked up"))
    outage.hooks_for("gsx", signal, env={}, services_dir=_svc(tmp_path, _BOTH))


def test_hooks_for_restart_without_the_hook_raises(tmp_path):
    with pytest.raises(OutageError, match="gsx has no restart_in_place"):
        outage.hooks_for("gsx", "RESTART", env={}, services_dir=_svc(tmp_path, ""))
