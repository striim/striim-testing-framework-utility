"""How a bounded command's process group is ended (``dockercmd._spawn``, ``_stop``, ``_drain``).

``_stop`` SIGKILLs the group the anchor holds, retries, and reaps the anchor and the command; ``_drain``
collects what a stopped command wrote. A regression leaves processes running after the call or hangs it.
No Docker: each command is a small script in ``tmp_path``. Most tests drive ``_spawn``/``_stop`` directly
and wait for the command to say it is ready, so a loaded host only makes them slower, never wrong.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import psutil
import pytest

from livetest import dockercmd

DEADLINE_S = 30.0


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_until(cond, what: str, deadline_s: float = DEADLINE_S) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    pytest.fail(what)


def _ready(d):
    _wait_until(lambda: (d / "ready").exists(), "the command never said it was ready")


def _pid(d, name: str) -> int:
    return int((d / name).read_text().split()[0])


def _kill(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pids(d):
    return [_pid(d, p.name) for p in d.iterdir() if p.name in ("command", "grandchild") and p.read_text().strip()]


def _in_thread(fn, *args):
    """Run ``fn`` in a thread; return (thread, result box). A hung call fails the test instead of the run."""
    box = {}

    def body():
        try:
            box["value"] = fn(*args)
        except BaseException as e:     # noqa: BLE001 - handed back to the test
            box["error"] = e
    t = threading.Thread(target=body, daemon=True)
    t.start()
    t.join(DEADLINE_S)
    return t, box


# A command that ignores SIGTERM, starts a grandchild (in its group, as `docker` starts the Compose plugin)
# that ignores it too, writes a line, records both pids and waits.
_STUBBORN_SH = """#!/bin/sh
trap '' TERM HUP INT
d="$1"
sleep 120 &
echo $! > "$d/grandchild"
echo $$ > "$d/command"
echo "before the kill"
touch "$d/ready"
wait
sleep 120
"""

# A command that leaves the anchor's group (as `setsid` would), ignores SIGTERM, writes a line and sleeps.
_LEAVES_GROUP = """
import os, signal, sys, time
d = sys.argv[1]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
os.setpgid(0, 0)
open(os.path.join(d, "command"), "w").write(str(os.getpid()))
print("written before leaving", flush=True)
open(os.path.join(d, "ready"), "w").close()
time.sleep(120)
"""

# A command that starts a grandchild in a session of its own holding the command's stdout and stderr: a
# process out of the group's reach that keeps the pipe open.
_DETACHED_HOLDER = """
import os, subprocess, sys, time
d = sys.argv[1]
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
open(os.path.join(d, "grandchild"), "w").write(str(g.pid))
open(os.path.join(d, "command"), "w").write(str(os.getpid()))
print("written before the kill", flush=True)
open(os.path.join(d, "ready"), "w").close()
time.sleep(120)
"""


def _stubborn(tmp_path):
    script = tmp_path / "docker"            # stands in for the docker binary
    script.write_text(_STUBBORN_SH)
    script.chmod(0o755)
    return [str(script), str(tmp_path)]


def _py(tmp_path, body):
    return [sys.executable, "-c", body, str(tmp_path)]


# ------------------------------------------------------------------------------------------ _stop

def test_stop_kills_a_sigterm_ignoring_command_and_the_grandchild_in_its_group(tmp_path):
    proc, anchor = dockercmd._spawn(_stubborn(tmp_path), stdout=subprocess.DEVNULL)
    try:
        _ready(tmp_path)
        command, grandchild = _pid(tmp_path, "command"), _pid(tmp_path, "grandchild")
        assert os.getpgid(grandchild) == anchor.pid == os.getpgid(command)
        dockercmd._stop(anchor, proc)
        assert proc.returncode == -signal.SIGKILL
        assert anchor.returncode == -signal.SIGKILL and anchor.stdin.closed
        _wait_until(lambda: _gone(grandchild), f"the grandchild {grandchild} outlived _stop")
    finally:
        _kill(*_pids(tmp_path))
        if anchor.returncode is None:
            anchor.stdin.close()


@pytest.mark.parametrize("first", ["lost", "permission-error"])
def test_stop_retries_the_group_signal_when_the_first_does_not_land(tmp_path, monkeypatch, first):
    # "lost": the first signal raced a fork and missed the new process. "permission-error": macOS refuses
    # killpg while a killed group's members are being reaped. Either way the later signals end the group.
    proc, anchor = dockercmd._spawn(_stubborn(tmp_path), stdout=subprocess.DEVNULL)
    try:
        _ready(tmp_path)
        grandchild = _pid(tmp_path, "grandchild")
        real, calls = os.killpg, []

        def killpg(pgid, sig):
            calls.append(pgid)
            if len(calls) == 1:
                if first == "permission-error":
                    raise PermissionError(1, "Operation not permitted")
                return None
            return real(pgid, sig)
        monkeypatch.setattr(dockercmd.os, "killpg", killpg)
        t, box = _in_thread(dockercmd._stop, anchor, proc)
        assert not t.is_alive(), "_stop hung"
        assert "error" not in box, box.get("error")
        assert len(calls) >= 2
        _wait_until(lambda: _gone(grandchild), f"the grandchild {grandchild} outlived _stop")
    finally:
        monkeypatch.undo()
        _kill(*_pids(tmp_path))
        if anchor.returncode is None:
            anchor.stdin.close()


def test_stop_still_ends_and_reaps_the_anchor_when_no_group_signal_can_be_sent(tmp_path, monkeypatch):
    proc, anchor = dockercmd._spawn(_py(tmp_path, "import os, sys, time; "
                                        "open(os.path.join(sys.argv[1], 'ready'), 'w').close(); time.sleep(120)"))
    try:
        _ready(tmp_path)

        def killpg(pgid, sig):
            raise ProcessLookupError(3, "No such process")
        monkeypatch.setattr(dockercmd.os, "killpg", killpg)
        t, box = _in_thread(dockercmd._stop, anchor, proc)
        assert not t.is_alive(), "_stop hung waiting for an anchor nothing signalled"
        assert "error" not in box, box.get("error")
        assert anchor.returncode == -signal.SIGKILL and proc.returncode == -signal.SIGKILL
    finally:
        monkeypatch.undo()
        if anchor.returncode is None:
            anchor.stdin.close()         # the anchor then kills its own group
        if proc.returncode is None:
            _kill(proc.pid)


def test_stop_called_twice_signals_nothing_the_second_time(tmp_path, monkeypatch):
    proc, anchor = dockercmd._spawn(_stubborn(tmp_path), stdout=subprocess.DEVNULL)
    try:
        _ready(tmp_path)
        dockercmd._stop(anchor, proc)
        assert anchor.returncode is not None and proc.returncode is not None
        sent = []
        monkeypatch.setattr(dockercmd.os, "killpg", lambda pgid, sig: sent.append(("killpg", pgid)))
        monkeypatch.setattr(dockercmd.os, "kill", lambda pid, sig: sent.append(("kill", pid)))
        t, box = _in_thread(dockercmd._stop, anchor, proc)
        assert not t.is_alive() and "error" not in box, box.get("error")
        assert sent == [], f"a second _stop signalled a reaped pid: {sent}"
    finally:
        monkeypatch.undo()
        _kill(*_pids(tmp_path))


def test_a_command_that_left_the_group_is_still_killed_through_its_popen(tmp_path):
    proc, anchor = dockercmd._spawn(_py(tmp_path, _LEAVES_GROUP), stdout=subprocess.PIPE)
    try:
        _ready(tmp_path)
        command = _pid(tmp_path, "command")
        assert os.getpgid(command) != anchor.pid      # out of the group signal's reach
        t, box = _in_thread(dockercmd._stop, anchor, proc)
        assert not t.is_alive(), "_stop hung waiting for a command it never signalled"
        assert "error" not in box, box.get("error")
        assert proc.returncode == -signal.SIGKILL
        assert dockercmd._drain(proc) == b"written before leaving\n"
    finally:
        _kill(*_pids(tmp_path))
        if anchor.returncode is None:
            anchor.stdin.close()


def test_a_timed_out_run_whose_command_left_the_group_returns_and_leaves_nothing(tmp_path):
    t, box = _in_thread(lambda: dockercmd.run(_py(tmp_path, _LEAVES_GROUP), timeout=5.0))
    try:
        assert not t.is_alive(), "run hung on a command that left the group"
        assert "error" not in box, box.get("error")
        assert box["value"].returncode == dockercmd.TIMED_OUT
        if (tmp_path / "command").exists():
            command = _pid(tmp_path, "command")
            _wait_until(lambda: _gone(command), f"the command {command} outlived run")
    finally:
        _kill(*_pids(tmp_path))


# ------------------------------------------------------------------------------------------ _drain

def test_output_written_before_the_kill_is_delivered(tmp_path):
    proc, anchor = dockercmd._spawn(_stubborn(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _ready(tmp_path)
        dockercmd._stop(anchor, proc)
        t, box = _in_thread(dockercmd._drain, proc)
        assert not t.is_alive() and "error" not in box, box.get("error")
        assert box["value"] == b"before the kill\n"
    finally:
        _kill(*_pids(tmp_path))
        if anchor.returncode is None:
            anchor.stdin.close()


def test_a_checked_run_that_times_out_carries_the_output_written_before_it(tmp_path):
    with pytest.raises(dockercmd.DockerTimeout) as e:
        dockercmd.run(_stubborn(tmp_path), timeout=5.0, check=True)
    try:
        assert e.value.returncode == dockercmd.TIMED_OUT
        if (tmp_path / "ready").exists():        # a host too loaded to reach the echo in 5 s has nothing to show
            assert e.value.stdout == "before the kill\n"
            grandchild = _pid(tmp_path, "grandchild")
            _wait_until(lambda: _gone(grandchild), f"the grandchild {grandchild} outlived run")
    finally:
        _kill(*_pids(tmp_path))


def test_drain_gives_up_on_a_pipe_a_detached_process_still_holds_and_closes_it(tmp_path):
    proc, anchor = dockercmd._spawn(_py(tmp_path, _DETACHED_HOLDER), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
    try:
        _ready(tmp_path)
        grandchild = _pid(tmp_path, "grandchild")
        dockercmd._stop(anchor, proc)
        assert not _gone(grandchild)                 # out of reach: it still holds the pipe
        t, box = _in_thread(dockercmd._drain, proc)
        assert not t.is_alive(), "_drain waited for a pipe a detached process holds"
        assert "error" not in box, box.get("error")
        assert box["value"] is None
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        _kill(*_pids(tmp_path))
        if anchor.returncode is None:
            anchor.stdin.close()


# ------------------------------------------------------------------------------------------ _spawn

def test_a_command_that_cannot_start_leaves_no_anchor_running(tmp_path, monkeypatch):
    real, started = subprocess.Popen, []

    class Recording(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)
    monkeypatch.setattr(subprocess, "Popen", Recording)
    try:
        with pytest.raises(FileNotFoundError):
            dockercmd.run([str(tmp_path / "no-such-docker"), "ps"], timeout=30)
        monkeypatch.undo()
        assert len(started) == 1 and started[0].args == dockercmd._ANCHOR
        anchor = started[0]
        assert anchor.returncode is not None, "the anchor of a command that never started was left running"
        assert anchor.stdin.closed
        assert _gone(anchor.pid)
    finally:
        monkeypatch.undo()
        for p in started:
            if p.returncode is None:
                p.stdin.close()
                p.wait(timeout=DEADLINE_S)
