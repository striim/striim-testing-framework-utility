"""gRPC's fork log line must not fail an inventory listing (field, 2026-10-27).

With the Spanner Python client loaded, gRPC writes a line such as
``I1027 20:56:25.501391 26618724 ev_poll_posix.cc:587] FD from fork parent still in poll list``
into the stderr of every subprocess the test process starts. ``docker_ls`` read any stderr line
other than "No such file or directory" as an unreadable listing, so every Spanner live case failed
at its reset step.
"""
import os
import subprocess
import sys
from types import SimpleNamespace

from livetest import ownership

GRPC_LINE = ("I1027 20:56:25.501391 26618724 ev_poll_posix.cc:587] "
             "FD from fork parent still in poll list: fd(18, generation: 1)")


def test_a_grpc_fork_line_does_not_make_a_listing_unreadable(monkeypatch):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{ownership._LS_DONE}\n",
                               stderr=f"{GRPC_LINE}\nls: cannot access '/opt/x*': No such file or directory\n")
    monkeypatch.setattr(ownership.subprocess, "run", fake_run)
    assert ownership.docker_ls("/opt/x*", nodes=["n1"]) == []


def test_any_other_stderr_still_makes_a_listing_unreadable(monkeypatch):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=f"{ownership._LS_DONE}\n",
                               stderr="ls: /opt: Permission denied\n")
    monkeypatch.setattr(ownership.subprocess, "run", fake_run)
    try:
        ownership.docker_ls("/opt/x*", nodes=["n1"])
    except ownership.InventoryError as e:
        assert "Permission denied" in str(e)
    else:
        raise AssertionError("an ls error must still make the listing unreadable")


def _fork_support_after_import(env):
    out = subprocess.run([sys.executable, "-c",
                          "import os, livetest; print(os.environ.get('GRPC_ENABLE_FORK_SUPPORT'))"],
                         capture_output=True, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)),
                         check=True)
    return out.stdout.strip()


def test_importing_livetest_turns_grpc_fork_support_off_by_default():
    env = {k: v for k, v in os.environ.items() if k != "GRPC_ENABLE_FORK_SUPPORT"}
    assert _fork_support_after_import(env) == "false"


def test_a_users_grpc_fork_setting_is_kept():
    env = dict(os.environ, GRPC_ENABLE_FORK_SUPPORT="1")
    assert _fork_support_after_import(env) == "1"
