import json

import pytest

from livetest.assertions import AssertionFailed
from livetest.assertions.json import (
    JsonSpecError,
    _diff_path,
    _load_expected,
    assert_json,
    json_equal,
    parse_json_specs,
)


# ---- _diff_path / json_equal ---------------------------------------------------------


def test_identical_objects_equal():
    a = {"a": 1, "b": {"c": [1, 2]}}
    e = {"a": 1, "b": {"c": [1, 2]}}
    ok, diff = json_equal(a, e)
    assert ok and diff is None


def test_reordered_object_keys_equal():
    a = {"a": 1, "b": 2}
    e = {"b": 2, "a": 1}
    ok, diff = json_equal(a, e)
    assert ok and diff is None


def test_reordered_array_elements_equal():
    a = [1, 2, 3]
    e = [3, 1, 2]
    ok, diff = json_equal(a, e)
    assert ok and diff is None


def test_differing_leaf_returns_path():
    a = {"questions": [{"answers": [{"RESPONSE": None}]}]}
    e = {"questions": [{"answers": [{"RESPONSE": "R1"}]}]}
    ok, diff = json_equal(a, e)
    assert not ok
    assert diff == 'questions[0].answers[0].RESPONSE (expected \'R1\', got None)'


def test_missing_array_element_different_lengths_not_equal():
    a = [1, 2]
    e = [1, 2, 3]
    ok, diff = json_equal(a, e)
    assert not ok
    assert "length" in diff


def test_extra_object_key_not_equal():
    a = {"a": 1, "b": 2}
    e = {"a": 1}
    ok, diff = json_equal(a, e)
    assert not ok
    assert "unexpected key" in diff


def test_string_vs_number_not_equal():
    ok, diff = json_equal("1", 1)
    assert not ok
    assert diff is not None


def test_nested_array_in_array_equal_when_key_reordered():
    a = {
        "ID": "1",
        "questions": [
            {"Q": "q2", "answers": [{"A": "a2"}, {"A": "a1"}]},
            {"Q": "q1", "answers": [{"A": "a1"}]},
        ],
    }
    e = {
        "ID": "1",
        "questions": [
            {"Q": "q1", "answers": [{"A": "a1"}]},
            {"Q": "q2", "answers": [{"A": "a1"}, {"A": "a2"}]},
        ],
    }
    ok, diff = json_equal(a, e)
    assert ok and diff is None


# ---- parse_json_specs -----------------------------------------------------------------


def _base_spec(**overrides):
    # `key` is pre-normalized to a list here (as parse_json_specs would leave it) since
    # _load_expected/assert_json are exercised directly in these tests, bypassing parse.
    spec = {
        "target": "user_data",
        "db": "spanner-google",
        "column": "user_responses",
        "match": "expected/user_data.json",
        "key": ["user_id"],
    }
    spec.update(overrides)
    return spec


def test_parse_json_specs_normalizes_scalar_key_to_list():
    raw = _base_spec(key="user_id")
    specs = parse_json_specs([raw])
    assert specs[0]["key"] == ["user_id"]


def test_parse_json_specs_accepts_list_key():
    specs = parse_json_specs([_base_spec(key=["a", "b"])])
    assert specs[0]["key"] == ["a", "b"]


@pytest.mark.parametrize("field", ["target", "db", "column", "match"])
def test_parse_json_specs_missing_required_string_field_raises(field):
    spec = _base_spec()
    del spec[field]
    with pytest.raises(JsonSpecError):
        parse_json_specs([spec])


def test_parse_json_specs_missing_key_raises():
    spec = _base_spec()
    del spec["key"]
    with pytest.raises(JsonSpecError):
        parse_json_specs([spec])


def test_parse_json_specs_empty_key_list_raises():
    with pytest.raises(JsonSpecError):
        parse_json_specs([_base_spec(key=[])])


def test_parse_json_specs_non_list_raw_raises():
    with pytest.raises(JsonSpecError):
        parse_json_specs({"target": "x"})


# ---- _load_expected ---------------------------------------------------------------


def test_load_expected_single_row(tmp_path):
    golden = [{"user_id": "1", "user_responses": {"ID": "1"}}]
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps(golden))
    spec = _base_spec()
    expected = _load_expected(spec, tmp_path)
    assert expected == {("1",): {"ID": "1"}}


def test_load_expected_two_rows(tmp_path):
    golden = [
        {"user_id": "1", "user_responses": {"ID": "1"}},
        {"user_id": "2", "user_responses": {"ID": "2"}},
    ]
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps(golden))
    spec = _base_spec()
    expected = _load_expected(spec, tmp_path)
    assert expected == {("1",): {"ID": "1"}, ("2",): {"ID": "2"}}


def test_load_expected_renders_tokens(tmp_path):
    from livetest.assertions.json import _load_expected
    p = tmp_path / "expected.json"
    p.write_text('[{"id": "${TID}", "val": 1}]')
    got = _load_expected({"match": "expected.json", "key": ["id"], "column": "val"},
                          tmp_path, tokens={"TID": "t1"})
    assert got == {("t1",): 1}


def test_load_expected_missing_file_raises(tmp_path):
    spec = _base_spec()
    with pytest.raises(JsonSpecError):
        _load_expected(spec, tmp_path)


def test_load_expected_empty_list_raises(tmp_path):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps([]))
    spec = _base_spec()
    with pytest.raises(JsonSpecError):
        _load_expected(spec, tmp_path)


def test_load_expected_not_a_list_raises(tmp_path):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps({"user_id": "1"}))
    spec = _base_spec()
    with pytest.raises(JsonSpecError):
        _load_expected(spec, tmp_path)


# ---- assert_json ------------------------------------------------------------------


class StubAdmin:
    def __init__(self, rows):
        self._rows = rows

    def select_json_rows(self, table, key_cols, column):
        return self._rows


def _write_golden(tmp_path, rows):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps(rows))


def test_assert_json_happy_path_returns_passed_records(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"ID": "1"}}])
    admin = StubAdmin([(("1",), {"ID": "1"})])
    spec = _base_spec()
    records = assert_json(admin, [spec], tmp_path, timeout=0, poll=0, db="spanner-google")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "json"
    assert rec["target"] == "user_data"
    assert rec["db"] == "spanner-google"


def test_assert_json_mismatch_raises_assertion_failed(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"ID": "1"}}])
    admin = StubAdmin([(("1",), {"ID": "WRONG"})])
    spec = _base_spec()
    with pytest.raises(AssertionFailed) as exc_info:
        assert_json(admin, [spec], tmp_path, timeout=0, poll=0)
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert "ID" in records[0]["detail"]


def test_assert_json_missing_key_raises_assertion_failed(tmp_path):
    _write_golden(tmp_path, [
        {"user_id": "1", "user_responses": {"ID": "1"}},
        {"user_id": "2", "user_responses": {"ID": "2"}},
    ])
    admin = StubAdmin([(("1",), {"ID": "1"})])
    spec = _base_spec()
    with pytest.raises(AssertionFailed) as exc_info:
        assert_json(admin, [spec], tmp_path, timeout=0, poll=0)
    assert "missing key" in str(exc_info.value)


def test_assert_json_extra_key_raises_assertion_failed(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"ID": "1"}}])
    admin = StubAdmin([(("1",), {"ID": "1"}), (("2",), {"ID": "2"})])
    spec = _base_spec()
    with pytest.raises(AssertionFailed) as exc_info:
        assert_json(admin, [spec], tmp_path, timeout=0, poll=0)
    assert "unexpected key" in str(exc_info.value)


def test_assert_json_requires_json_capable_admin(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"ID": "1"}}])
    spec = _base_spec()

    class NoJsonAdmin:
        pass

    with pytest.raises(JsonSpecError):
        assert_json(NoJsonAdmin(), [spec], tmp_path, timeout=0, poll=0, db="postgres")
