"""Cancelling `striim-test run`: the tier (and every process it started) gets to finish its own signal
cleanup after one Ctrl-C; a second Ctrl-C stops it; a SIGTERM sent to striim-test is passed on; a
cancelled tier ends the run."""
import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest
import time
from pathlib import Path
from types import SimpleNamespace

CLI = Path(__file__).resolve().parents[2] / "scripts" / "cli"

# The tier (pytest) and one process it started (an xdist worker), each with a slow cleanup handler.
CHILD = textwrap.dedent("""
    import os, signal, subprocess, sys, time
    out, cleanup_s, sigs = sys.argv[1], float(sys.argv[2]), sys.argv[3].split(",")
    def cleanup(*a):
        time.sleep(cleanup_s)
        open(out, "w").write("cleanup finished")
        sys.exit(2)
    for name in sigs:
        signal.signal(getattr(signal, name), cleanup)
    if not out.endswith(".worker"):
        open(out + ".tier", "w").write(str(os.getpid()))
        worker = subprocess.Popen([sys.executable, __file__, out + ".worker", str(cleanup_s), sys.argv[3]])
        while not os.path.exists(out + ".worker.ready"):
            time.sleep(0.02)
        open(out + ".pid", "w").write(str(worker.pid))
    open(out + ".ready", "w").write("")
    if len(sys.argv) > 4 and not out.endswith(".worker"):
        sys.exit(0)                     # the tier ends and leaves its worker behind
    time.sleep(60)
""")

DRIVER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    from striim_test import dispatch
    run, child, out, cleanup_s, sigs = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    dispatch.build_pytest_argv = lambda *a, **k: ([sys.executable, child, out, cleanup_s, sigs, *sys.argv[6:]],
        {"SLT_RUN_EPOCH": "e", "SLT_INVOCATION_ID": "i", "SLT_RUN_IDENTITY": "r"})
    o = dispatch.run_tier(SimpleNamespace(run=run), "live", run, label="live", collect_only=False)
    print(json.dumps({"code": o.code, "reason": o.reason}))
""")


def _start(tmp_path, cleanup_s, sigs, *extra, wait_ready=True):
    (tmp_path / "child.py").write_text(CHILD)
    (tmp_path / "driver.py").write_text(DRIVER)
    (tmp_path / "run").mkdir()
    out = tmp_path / "cleanup.txt"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(CLI), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(tmp_path / "run"),
                             str(tmp_path / "child.py"), str(out), str(cleanup_s), sigs, *extra],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True,     # its own group: the "terminal" the test signals
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    deadline = time.monotonic() + 20
    while wait_ready and not Path(str(out) + ".ready").exists():
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        time.sleep(0.05)
    return proc, out


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _finish(proc, out, timeout=30):
    try:
        so, se = proc.communicate(timeout=timeout)
    finally:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        tier = Path(str(out) + ".tier")
        if tier.exists():               # the tier runs in its own group
            try:
                os.killpg(int(tier.read_text()), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    worker = int(Path(str(out) + ".pid").read_text())
    return json.loads(so.strip().splitlines()[-1]), se, worker


def test_one_ctrl_c_lets_the_tier_and_its_workers_finish_their_cleanup(tmp_path):
    proc, out = _start(tmp_path, 1.5, "SIGINT")
    os.killpg(proc.pid, signal.SIGINT)                     # Ctrl-C: striim-test's foreground group
    result, err, _ = _finish(proc, out)
    assert result == {"code": 4, "reason": "interrupted"}
    assert out.read_text() == "cleanup finished"
    deadline = time.monotonic() + 10
    while not Path(str(out) + ".worker").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert Path(str(out) + ".worker").read_text() == "cleanup finished"
    assert err.count("cancelling: waiting for cleanup") == 1


def test_a_quick_second_ctrl_c_stops_the_tier_and_its_workers(tmp_path):
    proc, out = _start(tmp_path, 30, "SIGINT")
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(0.1)                                        # inside Popen.wait's 0.25 s SIGINT window
    os.killpg(proc.pid, signal.SIGINT)
    result, err, worker = _finish(proc, out)
    assert result == {"code": 4, "reason": "interrupted"}
    assert time.monotonic() - started < 15 and not out.exists()
    assert "stopping without waiting for cleanup" in err
    time.sleep(0.2)
    assert not _alive(worker)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_a_signal_ending_striim_test_reaches_the_tier_and_its_workers(tmp_path, sig):
    # SIGTERM: the console cancelling the run; SIGHUP: the terminal closed; SIGQUIT: Ctrl-backslash.
    proc, out = _start(tmp_path, 1.0, "SIGTERM")
    os.kill(proc.pid, sig)
    result, _, _ = _finish(proc, out)
    assert result == {"code": 4, "reason": "interrupted"}
    assert out.read_text() == "cleanup finished"
    deadline = time.monotonic() + 10
    while not Path(str(out) + ".worker").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert Path(str(out) + ".worker").read_text() == "cleanup finished"


def test_a_cancelled_tier_ends_the_run(monkeypatch, tmp_path):
    sys.path.insert(0, str(CLI))
    from striim_test import dispatch, runner, errors
    ran = []

    def run_tier(ctx, t, p, **kw):
        ran.append(t)
        return dispatch.TierOutcome(t, t, errors.CANCELLED, "interrupted", None, str(tmp_path))
    monkeypatch.setattr(dispatch, "_prepare", lambda args, origins: ("ctx", ["r"]))
    monkeypatch.setattr(dispatch, "_plan_checked", lambda args, ctx, runners: (
        [("live", tmp_path), ("integration", tmp_path)], [SimpleNamespace(name="r")]))
    monkeypatch.setattr(dispatch, "run_tier", run_tier)
    monkeypatch.setattr(runner, "execute", lambda *a, **k: ran.append("runner"))
    monkeypatch.setattr(dispatch, "_finish", lambda ctx, codes, parts: codes)
    args = SimpleNamespace(parallel=False, case=None, tier=None, dry_run=False, keep_resources=False)
    assert dispatch.cmd_run(args, {}) == [errors.CANCELLED]
    assert ran == ["live"]


def test_what_a_finished_tier_leaves_in_its_group_is_stopped(tmp_path):
    proc, out = _start(tmp_path, 1.0, "SIGTERM", "exit")
    try:
        proc.communicate(timeout=30)
        worker = int(Path(str(out) + ".pid").read_text())
        deadline = time.monotonic() + 5
        while _alive(worker) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(worker)
    finally:
        _finish(proc, out)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_on_linux_a_sigkilled_striim_test_still_signals_the_tier(tmp_path):
    proc, out = _start(tmp_path, 0.5, "SIGTERM")
    os.kill(proc.pid, signal.SIGKILL)
    try:
        deadline = time.monotonic() + 10
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert out.read_text() == "cleanup finished"
    finally:
        proc.communicate(timeout=10)
        tier = Path(str(out) + ".tier")
        try:
            os.killpg(int(tier.read_text()), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_an_interrupt_during_the_launch_leaves_no_signal_blocked_or_handler_replaced(monkeypatch):
    sys.path.insert(0, str(CLI))
    from striim_test import dispatch
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(dispatch.subprocess, "Popen", interrupted)
    assert dispatch.run_tier_process(["true"], "live") is None
    assert not signal.pthread_sigmask(signal.SIG_BLOCK, set()) & {signal.SIGINT, *before}
    assert {s: signal.getsignal(s) for s in before} == before


# A tier that runs a bounded Docker command (livetest.dockercmd), which runs in a process group of its own.
# The command starts a child that ignores SIGINT and SIGTERM, records its pid, acts after 3 s and keeps
# running. However striim-test ends the tier, the command and that child must not outlive it.
STUBBORN = textwrap.dedent("""
    import os, signal, sys, time
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(sys.argv[1], "a").write(str(os.getpid()) + chr(10))
    time.sleep(3)
    open(sys.argv[2], "w").write("acted")
    time.sleep(30)
""")

TIER_WITH_A_COMMAND = textwrap.dedent("""
    import signal, sys, threading, time
    from livetest import dockercmd
    how, rest = sys.argv[1], sys.argv[2:]
    argv = [sys.executable, "-c", "import os, subprocess, sys, time; subprocess.Popen([sys.executable] + sys.argv[1:]); "
            "open(sys.argv[2], 'a').write(str(os.getpid()) + chr(10)); time.sleep(60)"] + rest
    if how == "ignores-signals":         # only the cancel bound's SIGKILL ends this tier
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if how == "thread":                  # the interrupt lands in the main thread, not in the command's wait
        threading.Thread(target=dockercmd.run, args=(argv,), kwargs={"timeout": 60}, daemon=True).start()
        time.sleep(60)
    else:
        dockercmd.run(argv, timeout=60)
""")

TIER_DRIVER = textwrap.dedent("""
    import json, sys
    from striim_test import dispatch
    dispatch.TIER_STOP_WAIT_S = 0.5
    cancel_wait, tier = float(sys.argv[1]), sys.argv[2:]
    print(json.dumps({"rc": dispatch.run_tier_process([sys.executable, *tier], "live", cancel_wait=cancel_wait)}))
""")


def _gone(pid) -> bool:
    import psutil
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.parametrize("how,cancel_wait", [("main", 10.0), ("thread", 10.0), ("ignores-signals", 0.5)])
def test_a_cancelled_tier_leaves_no_part_of_a_docker_command_running(tmp_path, how, cancel_wait):
    # main: the tier's own interrupt handling stops the command; thread: the tier exits with the command
    # still running in another thread; ignores-signals: striim-test SIGKILLs the tier at the cancel bound.
    live = CLI.parent / "live"
    (tmp_path / "tier.py").write_text(TIER_WITH_A_COMMAND)
    (tmp_path / "driver.py").write_text(TIER_DRIVER)
    (tmp_path / "stubborn.py").write_text(STUBBORN)
    pids, acted = tmp_path / "pids", tmp_path / "acted"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(CLI), str(live), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(cancel_wait), str(tmp_path / "tier.py"),
                             how, str(tmp_path / "stubborn.py"), str(pids), str(acted)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    started = []
    try:
        deadline = time.monotonic() + 20
        while not (pids.exists() and len(pids.read_text().split()) >= 2):
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        started = [int(p) for p in pids.read_text().split()]
        os.killpg(proc.pid, signal.SIGINT)                 # Ctrl-C
        so, se = proc.communicate(timeout=30)
        assert json.loads(so.strip().splitlines()[-1]) == {"rc": None}, se
        deadline = time.monotonic() + 3
        while not all(_gone(p) for p in started) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert all(_gone(p) for p in started), started
        time.sleep(3)
        assert not acted.exists(), "a child of the tier's docker command survived the tier and acted"
    finally:
        for pid in started:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
