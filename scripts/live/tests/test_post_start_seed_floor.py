"""The post-start seed floor: RUNNING is not reading, so an undelayed seed waits."""
from livetest.assertions.smoke import SETTLE_SECONDS
from livetest.plugin import SEED_FLOOR_SECONDS, _post_start_seed_floor


def test_floor_counts_the_smoke_settle_already_spent():
    # Smoke waited SETTLE_SECONDS past RUNNING; the seed waits the rest of the floor.
    assert _post_start_seed_floor(False, False, SETTLE_SECONDS) == SEED_FLOOR_SECONDS - SETTLE_SECONDS


def test_no_smoke_waits_the_whole_floor():
    assert _post_start_seed_floor(False, False, 0.0) == SEED_FLOOR_SECONDS


def test_a_reader_that_reports_reading_needs_no_floor():
    assert _post_start_seed_floor(True, False, SETTLE_SECONDS) == 0.0


def test_a_lifecycle_that_proved_readiness_needs_no_floor():
    assert _post_start_seed_floor(False, True, 0.0) == 0.0


def test_a_settle_longer_than_the_floor_waits_nothing_more():
    assert _post_start_seed_floor(False, False, SEED_FLOOR_SECONDS + 3) == 0.0


def test_the_floor_is_what_the_old_settle_gave():
    # Before the settle went to 1 s, every seeded case started 5 s after RUNNING.
    assert SEED_FLOOR_SECONDS == 5.0
