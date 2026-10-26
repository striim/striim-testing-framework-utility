"""Cancelling `striim-test run`: the tier (and every process it started) gets to finish its own signal
cleanup after one Ctrl-C; a second Ctrl-C stops it; a SIGTERM sent to striim-test is passed on; a
cancelled tier ends the run."""
import json
import os
import signal
import subprocess
import sys
import textwrap
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
        worker = subprocess.Popen([sys.executable, __file__, out + ".worker", str(cleanup_s), sys.argv[3]])
        while not os.path.exists(out + ".worker.ready"):
            time.sleep(0.02)
        open(out + ".pid", "w").write(str(worker.pid))
    open(out + ".ready", "w").write("")
    time.sleep(60)
""")

DRIVER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    from striim_test import dispatch
    run, child, out, cleanup_s, sigs = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    dispatch.build_pytest_argv = lambda *a, **k: ([sys.executable, child, out, cleanup_s, sigs],
        {"SLT_RUN_EPOCH": "e", "SLT_INVOCATION_ID": "i", "SLT_RUN_IDENTITY": "r"})
    o = dispatch.run_tier(SimpleNamespace(run=run), "live", run, label="live", collect_only=False)
    print(json.dumps({"code": o.code, "reason": o.reason}))
""")


def _start(tmp_path, cleanup_s, sigs):
    (tmp_path / "child.py").write_text(CHILD)
    (tmp_path / "driver.py").write_text(DRIVER)
    (tmp_path / "run").mkdir()
    out = tmp_path / "cleanup.txt"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(CLI), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(tmp_path / "run"),
                             str(tmp_path / "child.py"), str(out), str(cleanup_s), sigs],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True,     # its own group: the "terminal" the test signals
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    deadline = time.monotonic() + 20
    while not Path(str(out) + ".ready").exists():
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
    worker = int(Path(str(out) + ".pid").read_text())
    try:
        so, se = proc.communicate(timeout=timeout)
    finally:
        for pid in (proc.pid, worker):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
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


def test_a_sigterm_to_striim_test_reaches_the_tier_and_its_workers(tmp_path):
    proc, out = _start(tmp_path, 1.0, "SIGTERM")
    os.kill(proc.pid, signal.SIGTERM)                      # e.g. the console cancelling the run
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
