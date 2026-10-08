import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.checkpoint_history import (
    CheckpointHistorySpecError, assert_checkpoint_history, parse_checkpoint_history_spec,
)
from livetest.resultschema import validate, SCHEMA_VERSION


class FakeStriim:
    """`checkpoint_history(app)`: one list per poll, consumed in order (the last one repeats
    once exhausted) so a test can script the platform recording its first checkpoint late."""

    def __init__(self, histories):
        self.histories = list(histories)
        self.polls = 0

    def checkpoint_history(self, app):
        self.polls += 1
        return self.histories.pop(0) if len(self.histories) > 1 else self.histories[0]


# ---- spec parsing --------------------------------------------------------------------

def test_parse_ok():
    assert parse_checkpoint_history_spec("nonempty") == "nonempty"
    assert parse_checkpoint_history_spec("empty") == "empty"


def test_parse_refuses_anything_else():
    for bad in ("Nonempty", "", None, True, ["nonempty"], {"expect": "nonempty"}):
        with pytest.raises(CheckpointHistorySpecError, match="must be one of"):
            parse_checkpoint_history_spec(bad)


# ---- the assertion ---------------------------------------------------------------------

def test_nonempty_polls_until_the_platform_records_the_first_checkpoint():
    fake = FakeStriim([[], [], [{"serialNo": 1, "applicationName": "NS.App"}]])
    records = assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=10, poll=0)
    assert fake.polls == 3
    assert len(records) == 1 and records[0]["status"] == "passed"
    assert records[0]["type"] == "checkpoint_history" and records[0]["target"] == "NS.App"
    validate({"schema_version": SCHEMA_VERSION, "tests": [{
        "name": "t", "nodeid": "t", "status": "passed", "topology": "single", "services": [],
        "duration": 0.0, "skip_reason": None, "assertions": records}]})


def test_nonempty_fails_at_the_deadline_when_nothing_was_ever_recorded():
    fake = FakeStriim([[]])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=0, poll=0)
    assert "shows empty within 0s, expected nonempty" in str(e.value)
    assert e.value.records[0]["status"] == "failed"


def test_empty_passes_when_the_platform_has_recorded_nothing():
    fake = FakeStriim([[]])
    records = assert_checkpoint_history(fake, "NS.App", "empty", timeout=0, poll=0)
    assert records[0]["status"] == "passed"


def test_empty_fails_once_a_checkpoint_shows_up():
    fake = FakeStriim([[{"serialNo": 1}]])
    with pytest.raises(AssertionFailed) as e:
        assert_checkpoint_history(fake, "NS.App", "empty", timeout=0, poll=0)
    assert "shows nonempty within 0s, expected empty" in str(e.value)


def test_status_probe_runs_every_poll():
    calls = []
    fake = FakeStriim([[{"serialNo": 1}]])
    assert_checkpoint_history(fake, "NS.App", "nonempty", timeout=10, poll=0,
                              status_probe=lambda: calls.append(1))
    assert calls == [1]
