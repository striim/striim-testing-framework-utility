import hashlib
import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.gcs import parse_gcs_specs, _check_spec, assert_gcs, GcsSpecError


class FakeGcs:
    def __init__(self, data):
        self._data = data
    def read_object_bytes(self, bucket, name):
        return self._data.get((bucket, name))


def test_parse_ok():
    parse_gcs_specs([{"bucket": "b", "object": "o", "content_hex": "ab"}])
    parse_gcs_specs([{"bucket": "b", "object": "o", "sha256": "x"}])
    parse_gcs_specs([{"bucket": "b", "object": "o", "size": 3}])


def test_parse_requires_bucket_object_and_a_check():
    with pytest.raises(GcsSpecError):
        parse_gcs_specs([{"object": "o", "content_hex": "ab"}])       # no bucket
    with pytest.raises(GcsSpecError):
        parse_gcs_specs([{"bucket": "b", "object": "o"}])              # no content/sha/size


def test_check_content_hex_pass_and_fail():
    a = FakeGcs({("b", "o"): bytes.fromhex("89ab")})
    assert _check_spec(a, {"bucket": "b", "object": "o", "content_hex": "89AB"}) is None
    assert _check_spec(a, {"bucket": "b", "object": "o", "content_hex": "0000"}) is not None


def test_check_absent_object():
    a = FakeGcs({})
    assert "not present" in _check_spec(a, {"bucket": "b", "object": "o", "content_hex": "89"})


def test_check_size_and_sha256():
    data = bytes.fromhex("89abcd")
    a = FakeGcs({("b", "o"): data})
    assert _check_spec(a, {"bucket": "b", "object": "o", "size": 3}) is None
    assert _check_spec(a, {"bucket": "b", "object": "o", "sha256": hashlib.sha256(data).hexdigest()}) is None
    assert _check_spec(a, {"bucket": "b", "object": "o", "size": 99}) is not None


# ---- structured per-assertion records -----------------------------------------------

def test_assert_gcs_success_returns_passed_record_with_bytes_snapshot():
    data = bytes.fromhex("89abcd")
    a = FakeGcs({("b", "o"): data})
    spec = {"bucket": "b", "object": "o", "size": 3}
    records = assert_gcs(a, [spec], timeout=1, poll=0, db="gcs")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "gcs"
    assert rec["target"] == "b/o"
    assert rec["db"] == "gcs"
    assert rec["spec"] == spec
    assert rec["expected"] == {"kind": "bytes", "size": 3, "truncated": False}
    assert rec["actual"] == {"kind": "bytes", "size": 3, "truncated": False}


def test_assert_gcs_failure_raises_assertion_failed_with_records():
    data = bytes.fromhex("89abcd")
    a = FakeGcs({("b", "o"): data})
    spec = {"bucket": "b", "object": "o", "size": 99}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_gcs(a, [spec], timeout=0, poll=0)
    assert str(exc_info.value) == "gcs assertion not satisfied within 0s: b/o: size 3 != 99"
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["expected"] == {"kind": "bytes", "size": 99, "truncated": False}
    assert records[0]["actual"] == {"kind": "bytes", "size": 3, "truncated": False}


def test_assert_gcs_content_hex_mismatch_reports_actual_byte_length_as_size():
    data = bytes.fromhex("89ab")
    a = FakeGcs({("b", "o"): data})
    spec = {"bucket": "b", "object": "o", "content_hex": "0000"}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_gcs(a, [spec], timeout=0, poll=0)
    records = exc_info.value.records
    assert "content_hex mismatch" in records[0]["detail"]
    assert records[0]["expected"] == {"kind": "bytes", "size": 2, "truncated": False}
    assert records[0]["actual"] == {"kind": "bytes", "size": 2, "truncated": False}


def test_assert_gcs_mixed_specs_report_both_statuses():
    a = FakeGcs({("b", "ok"): bytes.fromhex("89"), ("b", "bad"): bytes.fromhex("89")})
    specs = [
        {"bucket": "b", "object": "ok", "size": 1},
        {"bucket": "b", "object": "bad", "size": 99},
    ]
    with pytest.raises(AssertionFailed) as exc_info:
        assert_gcs(a, specs, timeout=0, poll=0)
    records = exc_info.value.records
    assert len(records) == 2
    by_target = {r["target"]: r for r in records}
    assert by_target["b/ok"]["status"] == "passed"
    assert by_target["b/bad"]["status"] == "failed"
