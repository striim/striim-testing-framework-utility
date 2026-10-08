import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.smoke import assert_smoke
from livetest.striim import StriimError

class StubClient:
    def __init__(self, running_ok=True, settle_status="RUNNING", raise_await=False):
        self._settle_status = settle_status
        self._raise_await = raise_await
    def await_running(self, app, timeout, poll=2.0, progress=None):
        if self._raise_await:
            raise StriimError("did not reach RUNNING")
    def current_status(self, app):
        return self._settle_status

def test_smoke_passes_when_running_and_stable():
    assert_smoke(StubClient(), "NS.App", timeout=30, settle_seconds=0)

def test_smoke_fails_when_never_running():
    # AssertionFailed IS an AssertionError, so the framework now wraps the StriimError
    # uniformly (see the structured-record tests below) while keeping the same detail text.
    with pytest.raises(AssertionFailed, match="did not reach RUNNING"):
        assert_smoke(StubClient(raise_await=True), "NS.App", timeout=30, settle_seconds=0)

def test_smoke_fails_when_unstable_after_settle():
    with pytest.raises(AssertionFailed, match="CRASH"):
        assert_smoke(StubClient(settle_status="CRASH"), "NS.App", timeout=30, settle_seconds=0)

# ---- structured per-assertion records -----------------------------------------------

def test_smoke_success_returns_one_passed_record_with_no_snapshots():
    records = assert_smoke(StubClient(), "NS.App", timeout=30, settle_seconds=0, db="postgres")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "smoke"
    assert rec["spec"] == {}
    assert rec["target"] is None
    assert rec["db"] == "postgres"
    assert rec["expected"] is None
    assert rec["actual"] is None

def test_smoke_await_running_failure_returns_failed_record():
    with pytest.raises(AssertionFailed) as exc_info:
        assert_smoke(StubClient(raise_await=True), "NS.App", timeout=30, settle_seconds=0)
    assert str(exc_info.value) == "did not reach RUNNING"
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["type"] == "smoke"
    assert records[0]["spec"] == {}
    assert records[0]["target"] is None
    assert records[0]["expected"] is None
    assert records[0]["actual"] is None
    assert records[0]["detail"] == "did not reach RUNNING"

def test_smoke_unstable_failure_returns_failed_record():
    with pytest.raises(AssertionFailed) as exc_info:
        assert_smoke(StubClient(settle_status="CRASH"), "NS.App", timeout=30, settle_seconds=0)
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert "CRASH" in records[0]["detail"]
