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


def test_a_waiting_reload_goes_before_tests_that_arrive_after_it(tmp_path):
    """Writer preference: overlapping tests must not starve a pending reload. flock descriptors from
    separate open() calls are independent even in one process, so threads stand in for processes."""
    import threading
    order, t0 = [], time.monotonic()

    def test_run(name, start, hold):
        time.sleep(start)
        with opregistry.in_use(tmp_path):
            order.append(name)
            time.sleep(hold)

    def reload(start):
        time.sleep(start)
        with opregistry.exclusive_reload(tmp_path):
            order.append("reload")

    threads = [threading.Thread(target=test_run, args=("first", 0.0, 0.6)),
               threading.Thread(target=reload, args=(0.2,)),
               threading.Thread(target=test_run, args=("late", 0.4, 0.6))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert order == ["first", "reload", "late"]
    assert time.monotonic() - t0 < 10


def test_a_test_held_behind_a_pending_reload_says_so(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(opregistry, "_GATE_QUIET_S", 0.1)
    notes, started = [], threading.Event()

    def reload():
        with opregistry.exclusive_reload(tmp_path):
            started.set()
            time.sleep(0.5)

    t = threading.Thread(target=reload)
    t.start()
    started.wait(5)
    with opregistry.in_use(tmp_path, progress=notes.append):
        pass
    t.join(5)
    assert len(notes) == 1 and "waiting for a pending OP/UDF jar reload" in notes[0]
    with opregistry.in_use(tmp_path, progress=notes.append):
        pass
    assert len(notes) == 1                  # an uncontended start is silent


def test_a_momentary_gate_holder_is_not_reported(tmp_path):
    import threading
    notes, started = [], threading.Event()

    def brief():
        with opregistry._flock(opregistry._gate(_lock_path(tmp_path)), fcntl.LOCK_EX):
            started.set()
            time.sleep(0.3)               # another test passing the gate, or a short reload

    t = threading.Thread(target=brief)
    t.start()
    started.wait(5)
    with opregistry.in_use(tmp_path, progress=notes.append):
        pass
    t.join(5)
    assert notes == []
