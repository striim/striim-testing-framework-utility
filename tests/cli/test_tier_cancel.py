"""Cancelling `striim-test run`: the tier gets to finish its own signal cleanup after one Ctrl-C; a
second Ctrl-C stops it now; a SIGTERM sent to striim-test alone is passed on to the tier."""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "scripts" / "cli"

CHILD = textwrap.dedent("""
    import signal, sys, time
    out, cleanup_s, sigs = sys.argv[1], float(sys.argv[2]), sys.argv[3].split(",")
    def cleanup(*a):
        time.sleep(cleanup_s)
        open(out, "w").write("cleanup finished")
        sys.exit(2)
    for name in sigs:
        signal.signal(getattr(signal, name), cleanup)
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
                            start_new_session=True)      # its own group: the "terminal" the test signals
    deadline = time.monotonic() + 20
    while not Path(str(out) + ".ready").exists():
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        time.sleep(0.05)
    return proc, out


def _finish(proc, timeout=30):
    try:
        so, se = proc.communicate(timeout=timeout)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return json.loads(so.strip().splitlines()[-1]), se


def test_one_ctrl_c_lets_the_tier_finish_its_cleanup(tmp_path):
    proc, out = _start(tmp_path, 1.5, "SIGINT")
    os.killpg(proc.pid, signal.SIGINT)                     # Ctrl-C: the whole foreground group
    result, err = _finish(proc)
    assert result == {"code": 4, "reason": "interrupted"}
    assert out.read_text() == "cleanup finished"
    assert "cancelling: waiting for cleanup" in err


def test_a_second_ctrl_c_stops_the_tier_now(tmp_path):
    proc, out = _start(tmp_path, 30, "SIGINT")
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(0.5)
    started = time.monotonic()
    os.kill(proc.pid, signal.SIGINT)
    result, err = _finish(proc)
    assert result == {"code": 4, "reason": "interrupted"}
    assert time.monotonic() - started < 10 and not out.exists()
    assert "stopping without waiting for cleanup" in err


def test_a_sigterm_to_striim_test_alone_is_passed_on(tmp_path):
    proc, out = _start(tmp_path, 1.0, "SIGTERM")
    os.kill(proc.pid, signal.SIGTERM)                      # e.g. the console cancelling the run
    result, _ = _finish(proc)
    assert result == {"code": 4, "reason": "interrupted"}
    assert out.read_text() == "cleanup finished"
