"""The integration session and compose locks live in the machine-wide lock dir that every user on the host
shares (livetest.lockdir), so they follow its file policy: made 0666 whatever the umask, opened without
O_CREAT when another user owns them, and an older version's lock of the invoking user widened. Made with the
default mode, a lock one user left behind (0644 under umask 022) refused every other user's session and
`start`/`stop` with PermissionError."""
from __future__ import annotations

import errno
import os
import stat
import sys

import pytest

from inttest import cli, plugin

from .test_plugin_wiring import _FakeConfigureConfig


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def shared_root(tmp_path, monkeypatch):
    """The shared default lock dir (not an explicit, private SLT_LOCK_DIR), under a restrictive umask."""
    from livetest import stack
    d = tmp_path / "slt-locks"
    monkeypatch.delenv("SLT_LOCK_DIR", raising=False)
    monkeypatch.delenv("INT_ALLOW_CONCURRENT_SESSIONS", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("INT_STACK_PREFIX", "ec")
    monkeypatch.setattr(stack, "_LOCK_DIR", d)
    monkeypatch.setattr(sys.modules["livetest.lockdir"], "_widened", set())
    monkeypatch.setattr(plugin, "_session_lock", None)
    old = os.umask(0o077)
    yield d
    os.umask(old)
    plugin._release_session_lock()


def _take_every_lock(monkeypatch):
    plugin._acquire_session_lock(_FakeConfigureConfig(perf=False))
    plugin._release_session_lock()
    with plugin._compose_filelock():
        pass
    monkeypatch.setattr(cli, "_resolve_services", lambda targets: [])
    assert cli.cmd_up(["x"]) == 0 and cli.cmd_down(["x"]) == 0


def test_the_session_and_compose_locks_are_made_for_every_user(shared_root, monkeypatch):
    _take_every_lock(monkeypatch)
    made = sorted(p.name for p in shared_root.iterdir() if p.is_file())
    assert made == [".ec-int-compose.lock", ".ec-int-session.lock"]
    assert [_mode(shared_root / n) for n in made] == [0o666, 0o666]


def test_an_older_versions_integration_locks_of_this_user_are_widened(shared_root):
    shared_root.mkdir()
    old = [shared_root / ".int-session.lock", shared_root / ".ec-int-compose.lock"]
    for p in old:
        p.write_text("")
        os.chmod(p, 0o644)
    plugin._session_lock_path()                     # the first use of the dir in this process
    assert [_mode(p) for p in old] == [0o666, 0o666]


def test_a_second_user_takes_the_locks_the_first_user_left(shared_root, monkeypatch):
    _take_every_lock(monkeypatch)                   # the first user's run
    real = os.open

    def protected_regular(path, flags, mode=0o777, *a, **k):
        # the kernel refuses O_CREAT on another user's file in a sticky world-writable dir
        if flags & os.O_CREAT and os.path.exists(path):
            raise PermissionError(errno.EACCES, "Permission denied (protected_regular)", str(path))
        return real(path, flags, mode, *a, **k)
    monkeypatch.setattr(os, "open", protected_regular)
    _take_every_lock(monkeypatch)                   # the second user's run on the same stack


def test_inttest_cli_makes_the_compose_lock_for_every_user(shared_root, monkeypatch):
    monkeypatch.setattr(cli, "_resolve_services", lambda targets: [])
    assert cli.cmd_down(["x"]) == 0                 # `stop` before any session made the lock
    assert _mode(shared_root / ".ec-int-compose.lock") == 0o666
