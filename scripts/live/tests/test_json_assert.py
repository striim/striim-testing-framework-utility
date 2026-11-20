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


def test_reordered_array_elements_differ_by_default():
    # Position is part of an array's value (a SQL ARRAY column, a list of steps).
    ok, diff = json_equal([1, 2, 3], [3, 1, 2])
    assert not ok and diff == "[0] (expected 3, got 1)"


def test_root_array_order_is_ignored_when_listed_unordered():
    assert json_equal([1, 2, 3], [3, 1, 2], unordered=["$"]) == (True, None)
    assert json_equal([1, 2, 2], [2, 1, 1], unordered=["$"])[0] is False     # a multiset, not a set


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


@pytest.mark.parametrize("actual, expected", [
    (1, True), (0, False), (True, 1), ({"flag": 1}, {"flag": True}), ([0, 1], [False, True]),
    ({"a": [{"b": 1}]}, {"a": [{"b": True}]})])
def test_bool_is_not_equal_to_int_at_any_depth(actual, expected):
    ok, diff = json_equal(actual, expected)
    assert not ok and diff


def test_bool_equals_bool():
    assert json_equal({"a": [True, False]}, {"a": [True, False]}) == (True, None)
    assert json_equal({"a": [True, False]}, {"a": [False, True]}, unordered=["a"]) == (True, None)


@pytest.mark.parametrize("actual, expected", [
    (1.0, 1), (1, 1.0), ({"n": 2.0}, {"n": 2}), ([1, 2.0], [1, 2]), ({"a": [{"b": 0}]}, {"a": [{"b": 0.0}]})])
def test_an_integer_is_not_equal_to_a_float_at_any_depth(actual, expected):
    ok, diff = json_equal(actual, expected)
    assert not ok and diff


def test_floats_and_integers_equal_their_own_type():
    assert json_equal({"f": 1.5, "i": 7, "z": 0.0}, {"f": 1.5, "i": 7, "z": 0.0}) == (True, None)


def test_unordered_path_covers_that_array_only():
    a = {"items": [{"id": 2, "sub": [1, 2]}, {"id": 1, "sub": [3, 4]}]}
    e = {"items": [{"id": 1, "sub": [3, 4]}, {"id": 2, "sub": [1, 2]}]}
    assert json_equal(a, e)[0] is False
    assert json_equal(a, e, unordered=["items"]) == (True, None)
    swapped = {"items": [{"id": 1, "sub": [4, 3]}, {"id": 2, "sub": [1, 2]}]}
    assert json_equal(a, swapped, unordered=["items"])[0] is False          # sub keeps its order
    assert json_equal(a, swapped, unordered=["items", "items[].sub"]) == (True, None)


def test_nested_array_in_array_differs_by_default_and_equal_when_both_paths_listed():
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
    assert json_equal(a, e)[0] is False
    assert json_equal(a, e, unordered=["questions"])[0] is False
    assert json_equal(a, e, unordered=["questions", "questions[].answers"]) == (True, None)


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


def test_parse_json_specs_accepts_unordered_paths():
    specs = parse_json_specs([_base_spec(unordered=["$", "items", "items[].subitems", "$[]", "m[][]"])])
    assert specs[0]["unordered"] == ["$", "items", "items[].subitems", "$[]", "m[][]"]


@pytest.mark.parametrize("bad", ["items", [], [""], ["items[0]"], ["a..b"], [".a"], ["a."],
                                 ["[]x"], ["a b"], ["items", "items"], [3]])
def test_parse_json_specs_refuses_a_bad_unordered(bad):
    with pytest.raises(JsonSpecError, match="unordered"):
        parse_json_specs([_base_spec(unordered=bad)])


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


def test_load_expected_refuses_two_rows_for_one_key(tmp_path):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps([
        {"user_id": "1", "user_responses": {"v": "first"}},
        {"user_id": "1", "user_responses": {"v": "second"}}]))
    with pytest.raises(JsonSpecError, match="key user_id='1' appears 2 times"):
        _load_expected(_base_spec(), tmp_path)


def test_load_expected_refuses_keys_equal_only_as_text(tmp_path):
    # Keys are compared as text (the read stringifies them), so 1 and "1" are the same row.
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps([
        {"user_id": 1, "user_responses": {}}, {"user_id": "1", "user_responses": {}}]))
    with pytest.raises(JsonSpecError, match="appears 2 times"):
        _load_expected(_base_spec(), tmp_path)


def test_load_expected_refuses_a_duplicate_member_name(tmp_path):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(
        '[{"user_id": "1", "user_responses": {"v": 1, "v": 2}}]')
    with pytest.raises(JsonSpecError, match="duplicate member 'v'"):
        _load_expected(_base_spec(), tmp_path)


def test_load_expected_refuses_an_unordered_path_naming_no_array(tmp_path):
    (tmp_path / "expected").mkdir()
    (tmp_path / "expected" / "user_data.json").write_text(json.dumps([
        {"user_id": "1", "user_responses": {"items": [{"subitems": [1]}], "n": 1}}]))
    _load_expected(_base_spec(unordered=["items", "items[].subitems"]), tmp_path)
    for bad in (["itemz"], ["n"], ["items[].subitemz"], ["$"]):
        with pytest.raises(JsonSpecError, match="names no array"):
            _load_expected(_base_spec(unordered=bad), tmp_path)


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


def test_assert_json_fails_two_rows_for_one_key_in_the_read(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"ID": "1"}}])
    admin = StubAdmin([(("1",), {"ID": "1"}), (("1",), {"ID": "1"})])
    with pytest.raises(AssertionFailed, match="key user_id='1' appears 2 times") as e:
        assert_json(admin, [_base_spec()], tmp_path, timeout=0, poll=0)
    assert e.value.records[0]["status"] == "failed"


def test_assert_json_fails_a_reordered_array_and_passes_it_when_unordered(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"steps": ["a", "b", "c"]}}])
    admin = StubAdmin([(("1",), {"steps": ["c", "a", "b"]})])
    with pytest.raises(AssertionFailed, match=r"steps\[0\] \(expected 'a', got 'c'\)"):
        assert_json(admin, [_base_spec()], tmp_path, timeout=0, poll=0)
    records = assert_json(admin, [_base_spec(unordered=["steps"])], tmp_path, timeout=0, poll=0)
    assert records[0]["status"] == "passed"


# The review's two defect tests, inverted.
class _RowsAdmin:
    def __init__(self, rows):
        self.rows = rows

    def select_json_rows(self, target, key, column):
        return self.rows


def test_fwl8_array_order_duplicate_read_keys_and_float_for_int_fail(tmp_path):
    (tmp_path / "g.json").write_text('[{"id": 1, "doc": {"steps": ["a", "b", "c"], "n": 1}}]')
    spec = parse_json_specs([{"target": "t", "db": "spanner", "column": "doc", "match": "g.json", "key": "id"}])
    rows = [(("1",), {"steps": ["zzz"], "n": 9}),
            (("1",), {"steps": ["c", "a", "b"], "n": 1.0})]
    with pytest.raises(AssertionFailed, match="appears 2 times"):
        assert_json(_RowsAdmin(rows), spec, tmp_path, timeout=0.05, poll=0.01)
    with pytest.raises(AssertionFailed, match=r"steps\[0\]"):
        assert_json(_RowsAdmin([(("1",), {"steps": ["c", "a", "b"], "n": 1})]), spec, tmp_path,
                    timeout=0.05, poll=0.01)
    with pytest.raises(AssertionFailed, match=r"n \(expected 1, got 1\.0\)"):
        assert_json(_RowsAdmin([(("1",), {"steps": ["a", "b", "c"], "n": 1.0})]), spec, tmp_path,
                    timeout=0.05, poll=0.01)


def test_fwl8_golden_with_two_rows_for_one_key_is_refused(tmp_path):
    (tmp_path / "g.json").write_text('[{"id": 1, "doc": {"v": "first"}}, {"id": 1, "doc": {"v": "second"}}]')
    spec = parse_json_specs([{"target": "t", "db": "spanner", "column": "doc", "match": "g.json", "key": "id"}])
    with pytest.raises(JsonSpecError, match="appears 2 times"):
        assert_json(_RowsAdmin([(("1",), {"v": "second"})]), spec, tmp_path, timeout=0.05, poll=0.01)


# ---- stable_seconds: hold a passing JSON assertion for N seconds -------------------------------


class SequenceAdmin:
    """Each read returns the next state; the last one repeats. A state is a row list, or an
    exception instance to raise."""
    def __init__(self, *states):
        self.states, self.reads = list(states), 0

    def select_json_rows(self, table, key_cols, column):
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        if isinstance(state, Exception):
            raise state
        return state


_N1 = [(("1",), {"n": 1})]
_N2 = [(("1",), {"n": 2})]


def _n_golden(tmp_path):
    _write_golden(tmp_path, [{"user_id": "1", "user_responses": {"n": 1}}])


def test_parse_json_specs_accepts_stable_seconds():
    assert parse_json_specs([_base_spec(stable_seconds=0)])
    assert parse_json_specs([_base_spec(stable_seconds=2.5)])


@pytest.mark.parametrize("bad", [-1, "5", True, None, [5]])
def test_parse_json_specs_refuses_a_bad_stable_seconds(bad):
    with pytest.raises(JsonSpecError, match="stable_seconds"):
        parse_json_specs([_base_spec(stable_seconds=bad)])


def test_without_stable_seconds_a_late_json_change_is_never_read(tmp_path):
    # The default is unchanged: the first equal read passes.
    _n_golden(tmp_path)
    admin = SequenceAdmin(_N1, _N2)
    records = assert_json(admin, [_base_spec()], tmp_path, timeout=5, poll=0.01)
    assert records[0]["status"] == "passed" and admin.reads == 1


def test_stable_seconds_fails_a_json_value_overwritten_in_the_window(tmp_path):
    _n_golden(tmp_path)
    admin = SequenceAdmin(_N1, _N1, _N2)
    with pytest.raises(AssertionFailed) as ei:
        assert_json(admin, [_base_spec(stable_seconds=5)], tmp_path, timeout=5, poll=0.01)
    msg = str(ei.value)
    assert "passed, then failed within its stable_seconds window" in msg
    assert "expected 1, got 2" in msg
    assert admin.reads == 3
    assert [r["status"] for r in ei.value.records] == ["failed"]


def test_stable_seconds_fails_a_json_row_added_in_the_window(tmp_path):
    _n_golden(tmp_path)
    admin = SequenceAdmin(_N1, _N1 + [(("2",), {"n": 1})])
    with pytest.raises(AssertionFailed, match="unexpected key"):
        assert_json(admin, [_base_spec(stable_seconds=5)], tmp_path, timeout=5, poll=0.01)


def test_stable_seconds_passes_an_unchanged_json_value_held_through_the_window(tmp_path):
    import time
    _n_golden(tmp_path)
    admin = SequenceAdmin(_N1)
    t0 = time.monotonic()
    records = assert_json(admin, [_base_spec(stable_seconds=0.3)], tmp_path, timeout=5, poll=0.05)
    assert time.monotonic() - t0 >= 0.3
    assert admin.reads > 2
    assert records[0]["status"] == "passed"


def test_json_stable_seconds_is_the_longest_of_the_group(tmp_path):
    import time
    _n_golden(tmp_path)
    t0 = time.monotonic()
    assert_json(SequenceAdmin(_N1), [_base_spec(stable_seconds=0.3), _base_spec()], tmp_path,
                timeout=5, poll=0.05)
    assert time.monotonic() - t0 >= 0.3


def test_json_stable_seconds_runs_past_the_timeout_when_it_must(tmp_path):
    # As in assert.data and assert.file: the window starts at the first pass and is held in full.
    import time
    _n_golden(tmp_path)
    t0 = time.monotonic()
    assert_json(SequenceAdmin(_N1), [_base_spec(stable_seconds=0.3)], tmp_path, timeout=0, poll=0.05)
    assert time.monotonic() - t0 >= 0.3


def test_a_json_read_that_raises_in_the_window_is_not_a_pass(tmp_path):
    # A JSON read has no "not readable yet" state: a read that raises ends the assertion, so an
    # unreadable target in the window can neither pass it nor hold it open.
    _n_golden(tmp_path)
    admin = SequenceAdmin(_N1, RuntimeError("table gone"))
    with pytest.raises(RuntimeError, match="table gone"):
        assert_json(admin, [_base_spec(stable_seconds=5)], tmp_path, timeout=5, poll=0.01)
    assert admin.reads == 2
