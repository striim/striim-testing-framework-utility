"""Hermetic tests for the post-recover restore, seed, and settle sequence."""
from livetest.plugin import _restore_then_post_recover


class _Clock:
    def __init__(self):
        self.time = 0.0
        self.events = []

    def now(self):
        return self.time

    def sleep(self, seconds):
        assert seconds >= 0.0
        self.events.append(("sleep", seconds, self.time))
        self.time += seconds


def _callbacks(clock):
    def restore(settle):
        clock.events.append(("restore", settle, clock.now()))
        clock.time += 25.0
        clock.restore_returned_at = clock.now()

    def run_files(items):
        clock.events.append(("seed", list(items), clock.now()))

    return restore, run_files


def test_last_cycle_restores_then_seeds_at_offsets_and_settles():
    clock = _Clock()
    restore, run_files = _callbacks(clock)

    _restore_then_post_recover(
        restore,
        run_files,
        [("source", "first.sql", 2.0), ("source", "second.sql", 5.0)],
        settle=3.0,
        last=True,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert clock.events[0] == ("restore", 0.0, 0.0)
    seeds = [event for event in clock.events if event[0] == "seed"]
    assert [event[1] for event in seeds] == [
        [("source", "first.sql")],
        [("source", "second.sql")],
    ]
    after_offsets = [2.0, 5.0]
    assert all(seed[2] >= clock.restore_returned_at + after
               for seed, after in zip(seeds, after_offsets))
    assert [event[2] for event in seeds] == [
        clock.restore_returned_at + after for after in after_offsets
    ]
    assert clock.events.index(seeds[0]) > 0
    last_seed = max(index for index, event in enumerate(clock.events) if event[0] == "seed")
    assert sum(event[1] for event in clock.events[last_seed + 1:]
               if event[0] == "sleep") == 3.0
    assert all(event[1] >= 0.0 for event in clock.events if event[0] == "sleep")


def test_last_cycle_without_seeds_restores_with_settle_and_does_not_wait():
    clock = _Clock()
    restore, run_files = _callbacks(clock)

    _restore_then_post_recover(
        restore,
        run_files,
        [],
        settle=7.0,
        last=True,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert clock.events == [("restore", 7.0, 0.0)]


def test_non_last_cycle_restores_without_seeding():
    clock = _Clock()
    restore, run_files = _callbacks(clock)

    _restore_then_post_recover(
        restore,
        run_files,
        [("source", "late.sql", 4.0)],
        settle=7.0,
        last=False,
        sleep=clock.sleep,
        now=clock.now,
    )

    assert clock.events == [("restore", 0.0, 0.0)]


def _reader(*statuses):
    """A status reader that returns each status in turn, then repeats the last."""
    seq = list(statuses)
    calls = []

    def read():
        calls.append(len(calls))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    read.calls = calls
    return read


def test_post_recover_settle_raises_when_the_app_crashes_during_it():
    import pytest
    from livetest.recovery import RecoveryError
    clock = _Clock()
    restore, run_files = _callbacks(clock)
    with pytest.raises(RecoveryError, match="CRASH"):
        _restore_then_post_recover(restore, run_files, [("source", "late.sql", 1.0)],
                                   settle=10.0, last=True, sleep=clock.sleep, now=clock.now,
                                   status=_reader("RUNNING", "RUNNING", "CRASH"))
    assert any(e[0] == "seed" for e in clock.events)


def test_post_recover_settle_after_a_sentinel_is_watched_too():
    import pytest
    from livetest.recovery import RecoveryError
    clock = _Clock()
    restore, run_files = _callbacks(clock)
    with pytest.raises(RecoveryError, match="HALT"):
        _restore_then_post_recover(restore, run_files, [], settle=6.0, last=True,
                                   sleep=clock.sleep, now=clock.now,
                                   after_restore=lambda: None, status=_reader("RUNNING", "HALT"))


def test_post_recover_settle_still_waits_the_full_settle_while_running():
    clock = _Clock()
    restore, run_files = _callbacks(clock)
    status = _reader("RUNNING")
    _restore_then_post_recover(restore, run_files, [("source", "late.sql", 2.0)], settle=3.0,
                               last=True, sleep=clock.sleep, now=clock.now, status=status)
    last_seed = max(i for i, e in enumerate(clock.events) if e[0] == "seed")
    assert sum(e[1] for e in clock.events[last_seed + 1:] if e[0] == "sleep") == 3.0
    assert len(status.calls) >= 2          # read before and after the settle's sleeps


def test_wait_before_a_later_interruption_watches_the_restored_app():
    import pytest
    from livetest.plugin import _wait_before_interrupt
    from livetest.recovery import RecoveryError
    clock = _Clock()
    with pytest.raises(RecoveryError, match="CRASH"):
        _wait_before_interrupt(8.0, status=_reader("RUNNING", "CRASH"),
                               sleep=clock.sleep, now=clock.now)


def test_wait_before_the_first_interruption_is_the_plain_timed_wait():
    from livetest.plugin import _wait_before_interrupt
    clock = _Clock()
    _wait_before_interrupt(5.0, status=None, sleep=clock.sleep, now=clock.now)
    assert clock.time == 5.0


def _recover_block():
    from pathlib import Path
    import livetest
    src = (Path(livetest.__file__).parent / "plugin.py").read_text()
    start = src.index("# RECOVERY PHASE (opt-in, `recover:`)")
    return src[start:src.index("# ACTION PHASE (opt-in, `action:`)", start)]


def test_plugin_watches_the_post_recover_settle_and_the_gap_between_cycles():
    # The executed path has no hermetic fixture; this pins the wiring the tests above exercise.
    import re
    block = _recover_block()
    call = block[block.index("_restore_then_post_recover("):]
    call = call[:call.index("report=lambda msg: _report(m.name, msg))")]
    assert re.search(r"status=\(lambda: client\.current_status\(_rc_app\)\)\s+if _rc\[\"expect_running\"\] else None", call)
    gap = block[block.index("_wait_before_interrupt("):]
    assert re.match(r"_wait_before_interrupt\(\s*_wait,\s*status=\(lambda: client\.current_status\(_rc_app\)\) if _i else None", gap)
    assert "time.sleep(min(2.0, _wait - _el))" not in block      # the blind gap loop is gone
    # restore() records what expect_running: false found
    assert "record=self._slt_records.append" in block[block.index("recovery.restore("):]
