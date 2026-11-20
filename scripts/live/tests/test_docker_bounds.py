"""Every Docker command the engines run is bounded, and so is every wait for a bring-up lock.

A hung Docker daemon used to hang the declaration that held the port's registry lock, a session teardown and
every server-file read, and a waiter for a bring-up lock waited forever. The scan below finds any
`subprocess.run` (or `check_output`/`check_call`/`call`) without a `timeout` in either engine; the only
exception is a build whose output streams to the terminal.
"""
from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from livetest import dockercmd

LIVE = Path(__file__).resolve().parents[1] / "livetest"
INTEG = Path(__file__).resolve().parents[2] / "integration" / "inttest"

# (file, enclosing function): builds whose output streams to the terminal, watched and stoppable by the user.
# Only the download and the image build may call it (test_only_the_download_and_the_image_build_use_...).
STREAMING_BUILDS = {("livetest/striim_provision.py", "_stream")}


def _untimed_calls(path: Path, rel: str):
    found = []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in ("run", "check_output", "check_call", "call")):
            kws = {k.arg for k in node.keywords}
            if "timeout" not in kws and None not in kws:
                found.append((rel, fn, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(ast.parse(path.read_text()), None)
    return found


def _scan():
    out = []
    for root in (LIVE, INTEG):
        for path in sorted(root.rglob("*.py")):
            out += _untimed_calls(path, f"{root.name}/{path.relative_to(root).as_posix()}")
    return out


def test_no_subprocess_call_without_a_timeout_outside_streaming_builds():
    untimed = [c for c in _scan() if (c[0], c[1]) not in STREAMING_BUILDS]
    assert untimed == []


def test_the_streaming_allow_list_names_only_calls_that_exist():
    present = {(c[0], c[1]) for c in _scan()}
    assert STREAMING_BUILDS <= present        # a stale entry would hide a new untimed call of that name


# ---------------------------------------------------------------- the runner

def _sleeper(seconds):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_a_command_that_outlives_its_bound_is_stopped_and_answers_like_a_failure():
    started = time.monotonic()
    r = dockercmd.run(_sleeper(30), timeout=0.3)
    assert time.monotonic() - started < 10
    assert r.returncode == dockercmd.TIMED_OUT and r.stdout == ""
    assert "did not finish within 0s and was stopped" in r.stderr and "Docker daemon" not in r.stderr


# A command that starts a child of its own (as `docker` starts the Compose plugin, or a post-up script a
# client), which records its pid and, after a delay, acts: a survivor of the timeout would still act.
_FORKS = """
import subprocess, sys
pids, acted = sys.argv[1], sys.argv[2]
child = subprocess.Popen([sys.executable, "-c",
    "import os, sys, time; open(sys.argv[1], 'a').write(str(os.getpid()) + chr(10)); time.sleep(2); "
    "open(sys.argv[2], 'w').write('acted')", pids, acted])
open(pids, "a").write(str(child.pid) + chr(10))
child.wait()
"""


def _forks(tmp_path):
    pids, acted = tmp_path / "pids", tmp_path / "acted"
    return [sys.executable, "-c", _FORKS, str(pids), str(acted)], pids, acted


def _wait_for_pids(pids, n=2):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if pids.exists() and len(pids.read_text().split()) >= n:
            return [int(p) for p in pids.read_text().split()]
        time.sleep(0.05)
    pytest.fail("the command never started its child")


def _gone(pid) -> bool:
    import psutil
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _assert_nothing_survives(pids, acted):
    started = _wait_for_pids(pids)
    time.sleep(3)                                 # past the child's delayed action
    assert not acted.exists(), "a child of the timed-out command survived and acted"
    assert all(_gone(pid) for pid in started), started


@pytest.mark.parametrize("kw", [{}, {"capture": False}, {"check": True}], ids=["captured", "terminal", "checked"])
def test_a_command_that_outlives_its_bound_is_stopped_with_every_child(tmp_path, kw):
    argv, pids, acted = _forks(tmp_path)
    started = time.monotonic()
    try:
        r = dockercmd.run(argv, timeout=1.0, **kw)
    except dockercmd.DockerTimeout:
        assert kw.get("check")
    else:
        assert not kw.get("check") and r.returncode == dockercmd.TIMED_OUT
    assert time.monotonic() - started < 10
    _assert_nothing_survives(pids, acted)


def test_an_interrupted_command_is_stopped_with_every_child(tmp_path):
    import signal
    import threading
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)   # whatever an earlier test left
    argv, pids, acted = _forks(tmp_path)
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            dockercmd.run(argv, timeout=60)
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, previous)
    _assert_nothing_survives(pids, acted)


def test_a_command_runs_in_a_process_group_of_its_own():
    # The group is what its cleanup kills, so it reaches processes whose parent has already exited. The
    # caller's own group is never signalled (striim-test's cancellation reaches the command another way:
    # tests/cli/test_tier_cancel.py).
    r = dockercmd.run([sys.executable, "-c", "import os; print(os.getpgid(0))"], timeout=30)
    assert r.returncode == 0 and int(r.stdout) != os.getpgid(0)


# Ownership that does not depend on ancestry. In each fixture a process the command started outlives the
# process that started it, so it has been reparented by the time the bound expires: the command itself
# exits at once and leaves a child holding its stdout, or an intermediate exits and leaves a grandchild.
# The survivor records its pid, acts after 3 s and then keeps running (and holding any pipe it inherited).
_SURVIVOR = ("import os, sys, time; open(sys.argv[1], 'a').write(str(os.getpid()) + chr(10)); time.sleep(3); "
             "open(sys.argv[2], 'w').write('acted'); time.sleep(30)")

_LEADER_EXITS = """
import os, subprocess, sys
pids, acted, survivor = sys.argv[1], sys.argv[2], sys.argv[3]
open(pids, "a").write(str(os.getpid()) + chr(10))
subprocess.Popen([sys.executable, "-c", survivor, pids, acted])
"""

_INTERMEDIATE_EXITS = """
import os, subprocess, sys, time
pids, acted, survivor = sys.argv[1], sys.argv[2], sys.argv[3]
open(pids, "a").write(str(os.getpid()) + chr(10))
subprocess.Popen([sys.executable, "-c", "import subprocess, sys; subprocess.Popen([sys.executable] + sys.argv[1:])",
                  "-c", survivor, pids, acted]).wait()
time.sleep(60)
"""


def _orphaning(tmp_path, script):
    pids, acted = tmp_path / "pids", tmp_path / "acted"
    return [sys.executable, "-c", script, str(pids), str(acted), _SURVIVOR], pids, acted


@pytest.mark.parametrize("script,kw", [(_LEADER_EXITS, {}), (_LEADER_EXITS, {"check": True}),
                                       (_INTERMEDIATE_EXITS, {}), (_INTERMEDIATE_EXITS, {"capture": False}),
                                       (_INTERMEDIATE_EXITS, {"check": True})],
                         ids=["leader-exits-captured", "leader-exits-checked", "intermediate-exits-captured",
                              "intermediate-exits-terminal", "intermediate-exits-checked"])
def test_a_process_whose_parent_exited_is_still_stopped_at_the_bound(tmp_path, script, kw):
    argv, pids, acted = _orphaning(tmp_path, script)
    started = time.monotonic()
    try:
        r = dockercmd.run(argv, timeout=1.0, **kw)
    except dockercmd.DockerTimeout:
        assert kw.get("check")
    else:
        assert not kw.get("check") and r.returncode == dockercmd.TIMED_OUT
    assert time.monotonic() - started < 2.5          # no wait for a pipe a survivor still held
    _assert_nothing_survives(pids, acted)


@pytest.mark.parametrize("script", [_LEADER_EXITS, _INTERMEDIATE_EXITS], ids=["leader-exits", "intermediate-exits"])
def test_a_streamed_process_whose_parent_exited_is_still_stopped_at_the_bound(tmp_path, script):
    argv, pids, acted = _orphaning(tmp_path, script)
    started = time.monotonic()
    assert dockercmd.stream(argv, lambda line: None, timeout=1.0) == dockercmd.TIMED_OUT
    assert time.monotonic() - started < 2.5          # the read does not wait for the survivor's pipe
    _assert_nothing_survives(pids, acted)


# A command that starts a helper with its output redirected, so that nothing waits for it, and returns
# successfully once the helper has recorded its pid. Left running, the helper would act after 3 s.
_LEAVES_A_HELPER = """
import os, subprocess, sys, time
pids, acted, survivor = sys.argv[1], sys.argv[2], sys.argv[3]
open(pids, "a").write(str(os.getpid()) + chr(10))
subprocess.Popen([sys.executable, "-c", survivor, pids, acted], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while len(open(pids).read().split()) < 2:
    time.sleep(0.02)
"""


@pytest.mark.parametrize("how", ["captured", "terminal", "stream"])
def test_a_helper_a_completed_command_left_running_is_stopped_when_it_returns(tmp_path, how):
    # Nothing the command started outlives the call: left in the command's process group, such a helper
    # would be out of reach of the tier's own cleanup once the call had returned.
    argv, pids, acted = _orphaning(tmp_path, _LEAVES_A_HELPER)
    started = time.monotonic()
    try:
        if how == "stream":
            code = dockercmd.stream(argv, lambda line: None, timeout=30)
        else:
            code = dockercmd.run(argv, timeout=30, check=True, capture=how == "captured").returncode
        assert code == 0 and time.monotonic() - started < 10
        _assert_nothing_survives(pids, acted)
    finally:
        _kill_all(pids)


@pytest.mark.parametrize("how", ["captured", "terminal"])
def test_a_wrapped_subprocess_run_does_not_turn_the_group_kill_off(tmp_path, monkeypatch, how):
    # A plugin or shim that wraps `subprocess.run` (pytest-subprocess, gevent, a timing shim) made
    # `_run_group` call the wrapper as if it were a test's stand-in, skipping the process group, the
    # bounded kill and the cleanup on return. `dockercmd.run` no longer calls `subprocess.run` at all.
    real, seen = subprocess.run, []

    def shim(*a, **k):
        seen.append(a[:1])
        return real(*a, **k)
    monkeypatch.setattr(subprocess, "run", shim)
    argv, pids, acted = _orphaning(tmp_path, _LEAVES_A_HELPER)
    try:
        assert dockercmd.run(argv, timeout=30, check=True, capture=how == "captured").returncode == 0
        _assert_nothing_survives(pids, acted)
        assert seen == []
    finally:
        _kill_all(pids)


# The caller of a command, in a session of its own (a stand-in for a tier). The command starts a child
# that ignores SIGINT and SIGTERM, records its pid, acts after 3 s and keeps running; the command itself
# dies of SIGINT (as `docker` does), so a group interrupt ends it before the child it leaves behind.
_STUBBORN = """
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "a").write(str(os.getpid()) + chr(10))
time.sleep(3)
open(sys.argv[2], "w").write("acted")
time.sleep(30)
"""

_CALLER = """
import sys
from livetest import dockercmd
how, rest = sys.argv[1], sys.argv[2:]
argv = [sys.executable, "-c", "import os, subprocess, sys, time; subprocess.Popen([sys.executable] + sys.argv[1:]); "
        "open(sys.argv[2], 'a').write(str(os.getpid()) + chr(10)); time.sleep(60)"] + rest
if how == "stream":
    dockercmd.stream(argv, lambda line: None, timeout=60)
else:
    dockercmd.run(argv, timeout=60)
"""


def _caller(tmp_path, how):
    pids, acted, stubborn = tmp_path / "pids", tmp_path / "acted", tmp_path / "stubborn.py"
    stubborn.write_text(_STUBBORN)
    proc = subprocess.Popen([sys.executable, "-c", _CALLER, how, str(stubborn), str(pids), str(acted)],
                            env=dict(os.environ, PYTHONPATH=str(LIVE.parent)), start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    _wait_for_pids(pids)
    return proc, pids, acted


def _kill_all(pids):
    for pid in [int(p) for p in pids.read_text().split()]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("how", ["run", "stream"])
def test_a_command_whose_caller_is_killed_is_stopped_with_every_process_it_started(tmp_path, how):
    # SIGKILL leaves the caller no cleanup of its own: what stops the command is that its owner is gone.
    proc, pids, acted = _caller(tmp_path, how)
    try:
        os.kill(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=30)
        _assert_nothing_survives(pids, acted)
    finally:
        _kill_all(pids)


def test_a_group_interrupt_that_ends_the_command_first_still_stops_the_child_it_left(tmp_path):
    # Ctrl-C at the terminal: SIGINT to the caller's whole process group.
    proc, pids, acted = _caller(tmp_path, "run")
    try:
        os.killpg(proc.pid, signal.SIGINT)
        out = proc.communicate(timeout=30)[0]
        assert "KeyboardInterrupt" in out, out
        _assert_nothing_survives(pids, acted)
    finally:
        _kill_all(pids)


# ---------------------------------------------------------------- whose processes cleanup may signal

def _unrelated_job():
    """Another job of this user, in a session of its own, with a child: what a reused pid could point at."""
    p = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; "
                          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                          "print(c.pid, flush=True); time.sleep(60)"],
                         stdout=subprocess.PIPE, text=True, start_new_session=True)
    return p, int(p.stdout.readline())


def _reused_pid_popen(monkeypatch, job):
    """``subprocess.Popen`` whose command's pid, once the command has been reaped, is the unrelated job's:
    the kernel handing the freed pid to another process, without waiting for it to happen."""
    real = subprocess.Popen

    class ReusedPid(real):
        def _wait(self, *a, **k):
            r = super()._wait(*a, **k)
            if self.returncode is not None and self.args and self.args[0] == sys.executable:
                self.pid = job.pid
            return r
    monkeypatch.setattr(subprocess, "Popen", ReusedPid)


def _assert_untouched(job, child):
    import psutil
    time.sleep(0.5)
    for pid in (job.pid, child):
        assert psutil.Process(pid).status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_STOPPED), pid


def _end(job, child):
    for pid in (child, job.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    job.wait()


def test_cleanup_after_the_command_was_reaped_signals_nothing_that_reused_its_pid(monkeypatch):
    # The text decoding of a captured output happens after the command has been reaped; it fails here, and
    # the cleanup on that exception must not adopt whatever now has the pid.
    job, child = _unrelated_job()
    try:
        _reused_pid_popen(monkeypatch, job)
        with pytest.raises(UnicodeDecodeError):
            dockercmd.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([255]))"], timeout=30)
        _assert_untouched(job, child)
    finally:
        _end(job, child)


@pytest.mark.parametrize("how", ["run", "stream"])
def test_ending_a_completed_command_signals_nothing_that_reused_its_pid(monkeypatch, how):
    # A completed command's group is ended too (what it left running), after the command has been reaped.
    job, child = _unrelated_job()
    argv = [sys.executable, "-c", "print('done')"]
    try:
        _reused_pid_popen(monkeypatch, job)
        if how == "run":
            assert dockercmd.run(argv, timeout=30).returncode == 0
        else:
            assert dockercmd.stream(argv, lambda line: None, timeout=30) == 0
        _assert_untouched(job, child)
    finally:
        _end(job, child)


def test_a_docker_command_that_times_out_says_so():
    msg = dockercmd.timed_out_message(["docker", "ps", "-a", "--filter", "x"], 60)
    assert msg == "`docker ps -a ...` did not finish within 60s and was stopped (is the Docker daemon responding?)"


def test_a_checked_command_that_times_out_raises_a_called_process_error_naming_the_bound():
    with pytest.raises(subprocess.CalledProcessError) as ei:
        dockercmd.run(_sleeper(30), timeout=0.3, check=True, capture=False)
    assert ei.value.returncode == dockercmd.TIMED_OUT and "did not finish within" in str(ei.value)


def test_a_command_inside_its_bound_is_returned_unchanged():
    r = dockercmd.run([sys.executable, "-c", "import sys; print('out'); sys.exit(3)"], timeout=30)
    assert r.returncode == 3 and r.stdout == "out\n"
    with pytest.raises(subprocess.CalledProcessError) as ei:
        dockercmd.run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30, check=True)
    assert ei.value.returncode == 3


def test_the_bounds_are_named_and_ordered():
    assert dockercmd.QUERY_TIMEOUT_S == 60.0
    assert dockercmd.COMPOSE_DOWN_TIMEOUT_S == 600.0
    assert dockercmd.COMPOSE_UP_TIMEOUT_S == 10800.0
    assert dockercmd.BUILD_TIMEOUT_S == 3600.0
    assert dockercmd.LOCK_WAIT_S == dockercmd.COMPOSE_DOWN_TIMEOUT_S + 2 * dockercmd.COMPOSE_UP_TIMEOUT_S


# ---------------------------------------------------------------- the streamed bring-up

_GRANDCHILD = ("import subprocess, sys, time; "
               "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
               "print('Container x Starting', flush=True); time.sleep(60)")


def test_a_streamed_command_and_its_children_are_killed_at_the_bound():
    # `docker compose` is a child of `docker`: killing only the first would leave the pipe open.
    lines = []
    started = time.monotonic()
    code = dockercmd.stream([sys.executable, "-c", _GRANDCHILD], lines.append, timeout=1.0)
    assert time.monotonic() - started < 20
    assert code == dockercmd.TIMED_OUT and lines == ["Container x Starting\n"]


def test_an_interrupted_stream_kills_the_command():
    def interrupt(line):
        raise KeyboardInterrupt
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        dockercmd.stream([sys.executable, "-c", _GRANDCHILD], interrupt, timeout=60)
    assert time.monotonic() - started < 20


def test_a_streamed_command_runs_in_a_process_group_of_its_own():
    lines = []
    code = dockercmd.stream([sys.executable, "-c", "import os; print(os.getpgid(0))"], lines.append, timeout=30)
    assert code == 0 and int(lines[0]) != os.getpgid(0)


def test_a_streamed_command_that_outlives_its_bound_is_stopped_with_every_child(tmp_path):
    argv, pids, acted = _forks(tmp_path)
    started = time.monotonic()
    assert dockercmd.stream(argv, lambda line: None, timeout=1.0) == dockercmd.TIMED_OUT
    assert time.monotonic() - started < 10
    _assert_nothing_survives(pids, acted)


def test_a_streamed_command_returns_its_exit_status():
    lines = []
    code = dockercmd.stream([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(2)"], lines.append,
                            timeout=30)
    assert code == 2 and lines == ["a\n", "b\n"]


# A command that writes a backlog at once, acts after 2 s and then exits 0. Its consumer takes 20 ms a line,
# so handing on the whole backlog would take about 8 s: the output is ready long before the bound expires.
_BACKLOG = """
import sys, time
for i in range(400):
    print(f"Container slt-{i:03d}  Waiting")
sys.stdout.flush()
time.sleep(2)
open(sys.argv[1], "w").write("acted")
"""


def test_a_slow_consumer_does_not_carry_a_stream_past_its_bound(tmp_path):
    acted, lines = tmp_path / "acted", []

    def slow(line):
        lines.append(line)
        time.sleep(0.02)
    started = time.monotonic()
    code = dockercmd.stream([sys.executable, "-c", _BACKLOG, str(acted)], slow, timeout=1.0)
    elapsed = time.monotonic() - started
    time.sleep(2.5)                                 # past the command's delayed action
    assert code == dockercmd.TIMED_OUT and not acted.exists(), "the command ran on past its bound"
    assert elapsed < 5                              # the bound, then a short delivery of what was written
    assert lines[0] == "Container slt-000  Waiting\n" and len(lines) < 400 and "not shown" in lines[-1]


# A command whose child leaves its process group (as a detached helper would) and keeps writing to the pipe,
# a line every 5 ms for 30 s: faster than the consumer, which takes 10 ms a line.
_DETACHED_WRITER = """
import subprocess, sys, time
w = subprocess.Popen([sys.executable, "-c",
                      "import time; [(print('Container x Waiting', flush=True), time.sleep(0.005)) for _ in range(6000)]"],
                     start_new_session=True)
open(sys.argv[1], "w").write(str(w.pid))
time.sleep(60)
"""


def test_output_that_keeps_arriving_cannot_carry_a_stream_past_its_bound(tmp_path):
    writer, lines = tmp_path / "writer", []

    def slow(line):
        lines.append(line)
        time.sleep(0.01)
    try:
        box = _waits_at_most(lambda: dockercmd.stream([sys.executable, "-c", _DETACHED_WRITER, str(writer)], slow,
                                                      timeout=1.0), seconds=8)
        assert box.get("value") == dockercmd.TIMED_OUT, box
        assert "not shown" in lines[-1]
    finally:
        if writer.exists():
            try:
                os.kill(int(writer.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_the_streamed_compose_up_is_bounded(monkeypatch, tmp_path):
    from livetest import services
    seen = {}

    def fake_stream(argv, on_line, *, timeout, env=None):
        seen.update(argv=argv, timeout=timeout)
        on_line("Container slt-postgres  Waiting\n")
        return dockercmd.TIMED_OUT
    monkeypatch.setattr(services.dockercmd, "stream", fake_stream)
    monkeypatch.setattr(services, "_preflight_profile", lambda defn: None)
    monkeypatch.setattr(services, "_compose_reset", lambda compose: None)
    monkeypatch.setattr(services, "_compose_env", lambda: {})
    monkeypatch.setattr(services.dockercmd, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    defn = SimpleNamespace(name="mysql", dir=tmp_path, compose="compose.yaml", container="slt-mysql")
    monkeypatch.setattr(services.drivers, "hook", lambda d, name: None)
    phases = []
    with pytest.raises(services.ServiceError, match="did not finish within 10800s"):
        services._default_compose_up(defn, progress=lambda name, phase: phases.append(phase))
    assert seen["timeout"] == dockercmd.COMPOSE_UP_TIMEOUT_S
    assert seen["argv"][:2] == ["docker", "compose"] and "up" in seen["argv"] and "--wait" in seen["argv"]
    assert phases                                       # the phase feedback still reaches the caller


# ---------------------------------------------------------------- bounded lock waits

def test_a_waiter_for_the_provisioning_lock_gives_up_naming_it(monkeypatch, tmp_path):
    from livetest import services, stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")         # never the machine-wide lock dir
    monkeypatch.setattr(dockercmd, "LOCK_WAIT_S", 0.3)
    lock = services._provision_key_lock("slt-oracle")     # held across another process's bring-up
    assert str(lock).startswith(str(tmp_path))
    stop = _held_elsewhere(lock)
    try:
        box = _waits_at_most(lambda: services.ensure_provisioned_once(
            "slt-oracle", lambda: pytest.fail("provisioned"), state_dir=tmp_path))
    finally:
        stop()
    assert isinstance(box.get("error"), services.ServiceError), box
    assert "not acquired within 0s" in str(box["error"]) and str(lock) in str(box["error"])


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


# ---------------------------------------------------------------- the cluster's compose up and down

def _cluster_boundary(monkeypatch, code=0):
    """Replace only the process boundary under cluster_up/cluster_down: whichever of `subprocess.run` or the
    bounded stream the call reaches, record its argv, cwd and bound and answer with exit status ``code``."""
    from livetest import striim_provision as sp
    calls = []

    def fake_run(argv, *a, **k):
        calls.append({"argv": list(argv), "cwd": k.get("cwd"), "timeout": k.get("timeout")})
        return subprocess.CompletedProcess(argv, code, "", "")

    def fake_stream(argv, on_line, *, timeout, env=None, cwd=None):
        calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        on_line("Container slt-striim  Started\n")
        return code
    monkeypatch.setattr(sp.subprocess, "run", fake_run)
    monkeypatch.setattr(sp.dockercmd, "stream", fake_stream)
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    monkeypatch.delenv("SLT_ENABLE_NODE_FAULTS", raising=False)
    from livetest import tryout
    monkeypatch.setattr(tryout, "configured", lambda: [])
    return sp, calls


def test_the_cluster_compose_up_is_bounded(monkeypatch, tmp_path):
    sp, calls = _cluster_boundary(monkeypatch)
    sp.cluster_up(tmp_path, {"STRIIM_VERSION": "5.4.0.6"})
    assert len(calls) == 1 and calls[0]["timeout"] == dockercmd.COMPOSE_UP_TIMEOUT_S
    assert calls[0]["argv"][:2] == ["docker", "compose"] and "up" in calls[0]["argv"]
    assert calls[0]["cwd"] == str(tmp_path)


def test_the_cluster_compose_down_is_bounded(monkeypatch, tmp_path):
    sp, calls = _cluster_boundary(monkeypatch)
    sp.cluster_down(tmp_path, {"STRIIM_VERSION": "5.4.0.6"})
    assert len(calls) == 1 and calls[0]["timeout"] == dockercmd.COMPOSE_DOWN_TIMEOUT_S
    assert calls[0]["argv"][:2] == ["docker", "compose"] and "down" in calls[0]["argv"]
    assert calls[0]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("step,bound", [("cluster_up", "10800s"), ("cluster_down", "600s")])
def test_a_cluster_compose_call_that_outlives_its_bound_raises_naming_it(monkeypatch, tmp_path, step, bound):
    # A RuntimeError, like a failed compose: bring-up reports "could not provision", teardown records a failure.
    sp, _ = _cluster_boundary(monkeypatch, code=dockercmd.TIMED_OUT)
    with pytest.raises(RuntimeError, match=f"did not finish within {bound} and was stopped"):
        getattr(sp, step)(tmp_path, {"STRIIM_VERSION": "5.4.0.6"})


def test_a_failed_cluster_compose_call_still_raises_its_status(monkeypatch, tmp_path):
    sp, _ = _cluster_boundary(monkeypatch, code=1)
    with pytest.raises(RuntimeError, match=r"command failed \(1\): docker compose"):
        sp.cluster_down(tmp_path, {"STRIIM_VERSION": "5.4.0.6"})


def test_only_the_download_and_the_image_build_use_the_unbounded_stream():
    # The allow-list exempts `_stream` by name, so a lifecycle call routed through it would be hidden.
    tree = ast.parse((LIVE / "striim_provision.py").read_text())
    users = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name != "_stream":
            if any(isinstance(n, ast.Name) and n.id == "_stream" for n in ast.walk(fn)):
                users.add(fn.name)
    assert users == {"ensure_deps", "ensure_image"}


def test_a_streamed_command_runs_in_the_given_directory(tmp_path):
    lines = []
    code = dockercmd.stream([sys.executable, "-c", "import os; print(os.getcwd())"], lines.append, timeout=30,
                            cwd=str(tmp_path))
    assert code == 0 and lines == [f"{tmp_path}\n"]


def test_a_hung_cluster_compose_call_and_its_children_are_stopped_at_the_bound(monkeypatch, tmp_path, capsys):
    # The real bounded runner, with a command that never finishes and leaves a child holding the pipe.
    from livetest import striim_provision as sp
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="did not finish within 1s"):
        sp._bounded_stream([sys.executable, "-c", _GRANDCHILD], cwd=str(tmp_path), timeout=1.0)
    assert time.monotonic() - started < 20
    assert "Container x Starting" in capsys.readouterr().out      # the output still reaches the terminal


# --- the Docker commands outside dockercmd go through it too (review N6) ---------------------------
# A lifecycle file read, an exact-data head, an ownership-ledger command, the evidence `docker inspect`,
# the Docker disk probe and the node-fault inventory ran `subprocess.run(..., timeout=...)`: a timeout
# there killed only the `docker` CLI, not what it started. Each now runs through dockercmd.run.

def _node_fault_inventory(monkeypatch):
    from livetest import node_fault
    monkeypatch.setattr(node_fault.stack, "striim_container", lambda: "primary")
    monkeypatch.setattr(node_fault.stack, "app_nodes", lambda: ["primary", "secondary"])
    monkeypatch.setattr(node_fault.stack, "agent_container", lambda: "agent")
    client = SimpleNamespace(list_deployment_groups=lambda timeout: [{"output": [{"default": {
        "actualServers": [{"name": "server_primary"}, {"name": "server_secondary"}]}}]}])
    with pytest.raises(node_fault.RecoveryError):
        node_fault.inventory(client, SimpleNamespace(mode="docker", url="http://test.invalid:19080"), timeout=30)


def _call_site(site, monkeypatch):
    from livetest import docker_disk, exactdata, infra, lifecycle, ownership
    argv = ["docker", "exec", "n1", "true"]
    if site == "lifecycle.timed_run":
        lifecycle.timed_run(5.0)(argv)
    elif site == "exactdata._run":
        exactdata._run(argv, 5.0)
    elif site == "ownership._docker":
        ownership._docker(argv)
    elif site == "infra._docker_inspect":
        infra._docker_inspect(argv)
    elif site == "docker_disk._default_run":
        docker_disk._default_run(argv)
    else:
        _node_fault_inventory(monkeypatch)


@pytest.mark.parametrize("site", ["lifecycle.timed_run", "exactdata._run", "ownership._docker",
                                  "infra._docker_inspect", "docker_disk._default_run", "node_fault.inventory"])
def test_each_docker_command_outside_dockercmd_runs_through_it(monkeypatch, site):
    calls = []

    def bypassed(*a, **k):
        raise AssertionError(f"{site} called subprocess.run directly")

    def run(argv, *, timeout=dockercmd.QUERY_TIMEOUT_S, **kw):
        calls.append((list(argv), timeout))
        return subprocess.CompletedProcess(argv, 1, "" if kw.get("capture", True) else b"", "")
    monkeypatch.setattr(subprocess, "run", bypassed)
    monkeypatch.setattr(dockercmd, "run", run)
    try:
        _call_site(site, monkeypatch)
    except AssertionError:
        raise
    except Exception:       # noqa: BLE001 - each site answers a failed command its own way
        pass
    assert calls and calls[0][0][0] == "docker" and 0 < calls[0][1] <= 120, calls
