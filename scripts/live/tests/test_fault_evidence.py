import pytest
from livetest import progress
from livetest.assertions import AssertionFailed, ExpectedAssertionFailure
class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds


def spec():
    return dict(kind="target-count", db="postgres-target", table="qatarget.rows",
                above=2, below=10, timeout="1s", poll="100ms")


def test_condition_rechecked_immediately_before_interruption():
    clock = Clock(); values = iter([3, 4, 5]); events = []; records = []
    def read(left): events.append("read"); return next(values)
    progress.at_fault(progress.normalize(spec()), read, lambda: events.append("interrupt"),
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert events == ["read", "read", "interrupt", "read"]
    actual = records[0]["actual"]["rows"][0]
    assert actual["value"] == 4 and actual["held"] is True
    assert actual["issuedAt"] == actual["observedAt"] == 0
    assert records[0]["type"] == "recovery" and records[0]["status"] == "passed"


def test_window_closed_between_wait_and_fault_fails_with_record():
    clock = Clock(); values = iter([3, 10]); records = []
    with pytest.raises(progress.ProgressError, match="fault-missed") as caught:
        progress.at_fault(progress.normalize(spec()), lambda left: next(values),
            lambda: pytest.fail("missed fault issued"), record=records.append,
            cycle=2, now=clock.now, sleep=clock.sleep)
    assert not isinstance(caught.value, AssertionFailed)
    actual = records[0]["actual"]["rows"][0]
    assert actual["value"] == 10 and actual["held"] is False and actual["issuedAt"] is None
    assert actual["cycle"] == 2 and records[0]["status"] == "failed"


def test_deadline_records_last_observation_and_never_xfails():
    clock = Clock(); records = []
    with pytest.raises(progress.ProgressError, match="fault-missed") as caught:
        progress.at_fault(progress.normalize(spec()), lambda left: 0,
            lambda: pytest.fail("fault without progress"), record=records.append,
            cycle=1, now=clock.now, sleep=clock.sleep)
    assert not isinstance(caught.value, ExpectedAssertionFailure)
    assert records[0]["actual"]["rows"][0]["value"] == 0
    assert records[0]["actual"]["rows"][0]["observedAt"] < 1


def test_interrupt_failure_cannot_be_recorded_as_passed():
    clock = Clock(); records = []
    def fail(): raise RuntimeError("command rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        progress.at_fault(progress.normalize(spec()), lambda left: 4, fail,
            record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert records[0]["status"] == "failed"
    assert records[0]["actual"]["rows"][0]["held"] is True
    assert records[0]["actual"]["rows"][0]["issuedAt"] == 0


def test_record_schema_accepts_recovery_fault_record():
    clock = Clock(); records = []
    progress.at_fault(progress.normalize(spec()), lambda left: 4, lambda: None,
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    assert records[0]["type"] == "recovery"
    from livetest.resultschema import _validate_assertion
    _validate_assertion(records[0], "fault")


def test_stop_that_drains_the_last_batch_fails_and_retains_both_boundaries():
    clock = Clock(); values = iter([3, 4, 10]); records = []
    with pytest.raises(progress.ProgressError, match='fault-missed'):
        progress.at_fault(progress.normalize(spec()), lambda left: next(values), lambda: None,
            record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    actual = records[0]['actual']['rows'][0]
    assert records[0]['status'] == 'failed' and actual['interrupted'] is True
    assert actual['windowOpened']['value'] == 3
    assert actual['value'] == 4 and actual['afterInterrupt']['value'] == 10


def test_success_retains_the_first_window_and_post_interrupt_observation():
    clock = Clock(); values = iter([3,4,5]); records = []
    progress.at_fault(progress.normalize(spec()), lambda left: next(values), lambda: None,
        record=records.append, cycle=1, now=clock.now, sleep=clock.sleep)
    actual = records[0]['actual']['rows'][0]
    assert actual['windowOpened']['value'] == 3 and actual['afterInterrupt']['value'] == 5
