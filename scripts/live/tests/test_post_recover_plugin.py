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
