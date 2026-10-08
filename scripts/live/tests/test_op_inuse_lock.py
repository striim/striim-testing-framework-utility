"""Hermetic coverage for the OP jar in-use / reload locks (Slice S8).

These primitives coordinate ACROSS PROCESSES, so the interesting properties cannot be
observed from a single process: `flock` is per open-file-description, and a process asking
for a lock it already holds on another descriptor blocks against itself rather than
succeeding. Every concurrency assertion here therefore uses a real child process.

The review that prompted this file noted -- correctly -- that the guard shipped with no
hermetic tests at all, and that the release-ordering bug in particular was a two-line slip a
single assertion would have caught.
"""

import fcntl
import multiprocessing as mp
import time

import pytest

from livetest import opregistry


def _try_lock(path_str, mode, out):
    """Attempt a NON-BLOCKING flock in a fresh process; report whether it was granted."""
    fh = open(path_str, "a+")
    try:
        fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
        out.put(True)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        out.put(False)
    finally:
        fh.close()


def _ask(path, mode, timeout=10):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_try_lock, args=(str(path), mode, q))
    p.start()
    try:
        return q.get(timeout=timeout)
    finally:
        p.join(timeout)


def _lock_path(tmp_path):
    return tmp_path / opregistry.stack.state_name(opregistry._USE_LOCK)


def test_many_tests_hold_in_use_at_once(tmp_path):
    """The shared lock must not serialise tests -- that is the whole point of SH."""
    with opregistry.in_use(tmp_path):
        assert _ask(_lock_path(tmp_path), fcntl.LOCK_SH) is True


def test_a_reload_waits_for_a_running_test(tmp_path):
    """The case the guard exists for: no jar swap while an app is running on it."""
    with opregistry.in_use(tmp_path):
        assert _ask(_lock_path(tmp_path), fcntl.LOCK_EX) is False


def test_a_running_test_cannot_start_during_a_reload(tmp_path):
    """And the converse: new tests are held off until the new bytes are loaded."""
    with opregistry.exclusive_reload(tmp_path):
        assert _ask(_lock_path(tmp_path), fcntl.LOCK_SH) is False


def test_locks_are_released_on_exit(tmp_path):
    with opregistry.in_use(tmp_path):
        pass
    assert _ask(_lock_path(tmp_path), fcntl.LOCK_EX) is True


def test_lock_is_released_when_the_body_raises(tmp_path):
    with pytest.raises(ValueError):
        with opregistry.in_use(tmp_path):
            raise ValueError("boom")
    assert _ask(_lock_path(tmp_path), fcntl.LOCK_EX) is True


def test_upgrading_shared_to_exclusive_self_deadlocks_and_reports_it(tmp_path):
    """The non-reentrancy trap, pinned deliberately.

    `_flock` open()s the file afresh per call, and flock treats descriptors from separate
    open() calls independently EVEN WITHIN ONE PROCESS -- so this blocks on itself. The
    contract is that it times out with an explanatory error instead of hanging the suite
    forever, and the poison-recovery path releases SH before asking for EX because of it.
    """
    with opregistry.in_use(tmp_path):
        started = time.monotonic()
        with pytest.raises(opregistry.LockTimeout) as e:
            with opregistry._flock(_lock_path(tmp_path), fcntl.LOCK_EX, timeout=0.5):
                pass
        assert time.monotonic() - started < 30          # bounded, not a hang
        assert "self-deadlock" in str(e.value)          # and it says why
