"""Fast, pure-Python tests for inttest.waevent (docs/INTEGRATION-TESTS.md).

No Java, no subprocess, no Docker -- these run everywhere, always.
"""
from __future__ import annotations

import copy

import pytest

from inttest import waevent

_BASE_EVENT = {
    "metadata": {"TableName": "CUSTOMER", "OperationName": "UPDATE"},
    "before": {"values": [101, "Record01", "old@example.com"], "present": [True, True, True]},
    "data": {"values": [101, "Record01", None], "present": [True, True, False]},
    "userdata": {"source": "test"},
}


def _events(*overrides):
    """One or more deep copies of _BASE_EVENT, each optionally mutated in place
    by an override callable."""
    out = []
    for override in overrides:
        e = copy.deepcopy(_BASE_EVENT)
        if override is not None:
            override(e)
        out.append(e)
    return out


# --- identical arrays / round trip -----------------------------------------

def test_identical_arrays_pass():
    emitted = _events(None)
    expected = _events(None)
    assert waevent.compare(emitted, expected) is None


def test_metadata_key_order_independence_passes():
    emitted = [{
        "metadata": {"OperationName": "UPDATE", "TableName": "CUSTOMER"},  # reordered
        "userdata": {},
    }]
    expected = [{
        "metadata": {"TableName": "CUSTOMER", "OperationName": "UPDATE"},
        "userdata": {},
    }]
    assert waevent.compare(emitted, expected) is None


def test_load_dump_round_trip(tmp_path):
    events = _events(None, None)
    text = waevent.dump(events)
    loaded_from_string = waevent.load(text)
    assert loaded_from_string == events

    path = tmp_path / "fixture.json"
    path.write_text(text)
    loaded_from_path = waevent.load(path)
    assert loaded_from_path == events

    loaded_from_str_path = waevent.load(str(path))
    assert loaded_from_str_path == events


# --- mismatches --------------------------------------------------------------

def test_length_mismatch_raises():
    emitted = _events(None)
    expected = _events(None, None)
    with pytest.raises(waevent.WAEventMismatch, match="event count mismatch"):
        waevent.compare(emitted, expected)


def test_data_value_diff_raises():
    def mutate(e):
        e["data"]["values"][1] = "Record02"
    emitted = _events(mutate)
    expected = _events(None)
    with pytest.raises(waevent.WAEventMismatch, match=r"data\.values\[1\]") as exc:
        waevent.compare(emitted, expected)
    assert "Record01" in str(exc.value)
    assert "Record02" in str(exc.value)


def test_presence_bit_diff_raises():
    def mutate(e):
        e["data"]["present"][2] = True  # emitted claims column 2 present; expected says absent
    emitted = _events(mutate)
    expected = _events(None)
    with pytest.raises(waevent.WAEventMismatch, match=r"data\.present\[2\]"):
        waevent.compare(emitted, expected)


def test_metadata_value_diff_raises():
    def mutate(e):
        e["metadata"]["TableName"] = "ORDERS"
    emitted = _events(mutate)
    expected = _events(None)
    with pytest.raises(waevent.WAEventMismatch, match="metadata") as exc:
        waevent.compare(emitted, expected)
    assert "CUSTOMER" in str(exc.value)
    assert "ORDERS" in str(exc.value)


def test_userdata_diff_raises():
    def mutate(e):
        e["userdata"]["source"] = "prod"
    emitted = _events(mutate)
    expected = _events(None)
    with pytest.raises(waevent.WAEventMismatch, match="userdata"):
        waevent.compare(emitted, expected)


def test_section_presence_diff_raises():
    def drop_before(e):
        del e["before"]
    emitted = _events(drop_before)   # emitted has no 'before' (e.g. an INSERT)
    expected = _events(None)         # expected has 'before' (e.g. an UPDATE)
    with pytest.raises(waevent.WAEventMismatch, match="before") as exc:
        waevent.compare(emitted, expected)
    assert "absent" in str(exc.value) and "present" in str(exc.value)


def test_null_distinct_from_absent():
    # Same value slot: one fixture marks it present with a null value, the
    # other marks it absent entirely -- presence must be checked, not `value
    # is None`, so this must be caught as a presence diff even though both
    # values are logically "null".
    emitted = [{
        "metadata": {"TableName": "CUSTOMER"},
        "data": {"values": [None], "present": [True]},
        "userdata": {},
    }]
    expected = [{
        "metadata": {"TableName": "CUSTOMER"},
        "data": {"values": [None], "present": [False]},
        "userdata": {},
    }]
    with pytest.raises(waevent.WAEventMismatch, match=r"data\.present\[0\]"):
        waevent.compare(emitted, expected)


# --- compare_indexed (PERF_SPEC.md §6 'sampled' mode) ------------------------

def test_compare_indexed_all_pairs_match():
    expected = _events(None, None, None)
    pairs = [(0, expected[0]), (2, expected[2])]  # sparse: skips index 1 entirely
    assert waevent.compare_indexed(pairs, expected) is None


def test_compare_indexed_mismatch_names_absolute_index_not_pair_position():
    expected = _events(None, None, None)

    def mutate(e):
        e["data"]["values"][1] = "Record02"
    bad = _events(mutate)[0]

    # `bad` is the FIRST element of `pairs` (ordinal 0) but is checked against
    # expected[2] (absolute index 2) -- the error must name 2, not 0.
    pairs = [(2, bad), (0, expected[0])]
    with pytest.raises(waevent.WAEventMismatch, match=r"event 2: data\.values\[1\]"):
        waevent.compare_indexed(pairs, expected)


def test_compare_indexed_out_of_range_index_raises():
    expected = _events(None)
    with pytest.raises(waevent.WAEventMismatch, match="out of range"):
        waevent.compare_indexed([(5, expected[0])], expected)


def test_compare_indexed_empty_pairs_is_a_noop():
    expected = _events(None)
    assert waevent.compare_indexed([], expected) is None


# --- ignore_fields / project ------------------------------------
#
# The capability exists because every reader in the fleet stamps a wall clock into its
# events, and assert.data is exact-match. The tests below carry the design decisions that
# are easy to regress: presence is still compared under an ignored index, an unmatched path
# is an ERROR rather than a no-op, and the two keys are inverses.

def _timestamped_pair():
    """Two arrays differing ONLY in a metadata wall clock and a data column."""
    def emitted(e):
        e["metadata"]["ReadTime"] = "2026-08-13T00:00:01Z"
        e["data"]["values"][2] = "emitted@example.com"
        e["data"]["present"][2] = True

    def expected(e):
        e["metadata"]["ReadTime"] = "2026-08-13T00:00:09Z"
        e["data"]["values"][2] = "expected@example.com"
        e["data"]["present"][2] = True

    return _events(emitted), _events(expected)


def test_ignore_fields_drops_a_map_key_and_an_array_index():
    emitted, expected = _timestamped_pair()
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, expected)          # exact-match still fails
    assert waevent.compare(
        emitted, expected, ignore_fields=["metadata.ReadTime", "data[2]"]) is None


def test_project_compares_only_the_named_fields():
    emitted, expected = _timestamped_pair()
    assert waevent.compare(
        emitted, expected, project=["metadata.TableName", "data[0]"]) is None


def test_project_still_fails_on_a_field_it_names():
    emitted, expected = _timestamped_pair()
    with pytest.raises(waevent.WAEventMismatch, match=r"metadata\['ReadTime'\]"):
        waevent.compare(emitted, expected, project=["metadata.ReadTime"])


def test_an_ignored_index_still_has_its_PRESENCE_compared():
    # The decision this pins: a wall-clock column varies in value and never in presence,
    # so ignoring the value must not also surrender the presence bitmap -- which is
    # precisely where this fleet's copyEvent bugs live.
    emitted, expected = _timestamped_pair()
    emitted[0]["data"]["present"][2] = False
    # Both volatile fields ignored, so the ONLY thing left that can fail is the presence
    # bitmap under the ignored index -- which is the point of the test.
    with pytest.raises(waevent.WAEventMismatch, match=r"data\.present\[2\]"):
        waevent.compare(emitted, expected, ignore_fields=["metadata.ReadTime", "data[2]"])


def test_a_path_matching_nothing_is_an_error_not_a_noop():
    # Without this, a renamed field leaves the case green while asserting less than its
    # author believes -- silently ignored assertion options.
    emitted, expected = _timestamped_pair()
    with pytest.raises(waevent.WAEventMismatch, match="matched no EMITTED event"):
        waevent.compare(emitted, expected, ignore_fields=["metadata.RenamedSinceThisWasWritten"])
    with pytest.raises(waevent.WAEventMismatch, match="matched no EMITTED event"):
        waevent.compare(emitted, expected, ignore_fields=["data[99]"])


def test_a_path_matching_only_SOME_events_is_accepted():
    # Multi-event fixtures legitimately differ in shape; matching at least one is the bar.
    def with_extra(e):
        e["userdata"]["GeneratedId"] = "abc"

    emitted = _events(with_extra, None)
    expected = _events(with_extra, None)
    emitted[0]["userdata"]["GeneratedId"] = "different"
    assert waevent.compare(emitted, expected, ignore_fields=["userdata.GeneratedId"]) is None


def test_the_two_keys_are_mutually_exclusive():
    emitted, expected = _timestamped_pair()
    with pytest.raises(ValueError, match="mutually exclusive"):
        waevent.compare(emitted, expected,
                        ignore_fields=["metadata.ReadTime"], project=["data[0]"])


@pytest.mark.parametrize("bad", ["bogus", "metadata", "data[1", "data[x]", "metadata.", ""])
def test_unparseable_paths_name_the_grammar(bad):
    with pytest.raises(ValueError, match="metadata"):
        waevent.parse_field_path(bad)


@pytest.mark.parametrize("path,expect", [
    ("metadata.TableName", ("metadata", "TableName")),
    ("userdata.source", ("userdata", "source")),
    ("data[0]", ("data", 0)),
    ("before[12]", ("before", 12)),
    ("metadata.a.dotted.key", ("metadata", "a.dotted.key")),
])
def test_parse_field_path_accepts_the_four_forms(path, expect):
    assert waevent.parse_field_path(path) == expect


def test_no_projection_keys_compares_exactly_as_before():
    emitted, expected = _timestamped_pair()
    with pytest.raises(waevent.WAEventMismatch):
        waevent.compare(emitted, expected, ignore_fields=None, project=None)


def test_a_field_the_operator_STOPPED_emitting_is_not_silently_ignorable():
    # The hole a previous revision had: reachability accepted a match in EITHER array, so a
    # stale fixture still naming a dropped field satisfied it -- and because an ignored key
    # also suppresses the missing-key check, the drop was reported as nothing at all.
    emitted = [{"metadata": {"TableName": "T"}}]
    expected = [{"metadata": {"TableName": "T", "ReadTime": "2026-08-13T00:00:00Z"}}]
    with pytest.raises(waevent.WAEventMismatch, match="stopped emitting"):
        waevent.compare(emitted, expected, ignore_fields=["metadata.ReadTime"])


def test_project_puts_non_projected_indices_ENTIRELY_out_of_scope():
    # Asymmetry with ignore_fields, pinned so it is a decision rather than an accident:
    # `project` means "only these are deterministic", so a non-projected index is not
    # compared at all -- not its value and not its presence.
    emitted = [{"data": {"values": [1, "x"], "present": [True, True]}}]
    expected = [{"data": {"values": [1, "x"], "present": [True, False]}}]
    assert waevent.compare(emitted, expected, project=["data[0]"]) is None

    # ...while an IGNORED index keeps its presence check. Same fixture, opposite key.
    with pytest.raises(waevent.WAEventMismatch, match=r"data\.present\[1\]"):
        waevent.compare(emitted, expected, ignore_fields=["data[1]"])


def test_project_scopes_out_the_sections_LENGTH_too():
    # The motivating case named in project's own docs: an operator whose non-projected columns
    # vary in shape. An earlier revision failed this on a section length mismatch before the
    # projection filter ever ran, so the documented semantic was not the implemented one.
    emitted = [{"data": {"values": [1, "x", "extra", "more"], "present": [True] * 4}}]
    expected = [{"data": {"values": [1, "x"], "present": [True, True]}}]
    assert waevent.compare(emitted, expected, project=["data[0]"]) is None


def test_a_projected_index_out_of_range_for_ONE_event_is_an_error():
    # Scoping the length out must not let a projection address a column that isn't there.
    # Reachability is the first line and fires when the index exists in NO event; this is the
    # second, for a multi-event fixture where the index exists in one event and not another --
    # reachability passes, and without this check event 1 would simply be skipped.
    emitted = [
        {"data": {"values": [1, "x", "y", "z"], "present": [True] * 4}},
        {"data": {"values": [2], "present": [True]}},
    ]
    expected = [
        {"data": {"values": [1, "x", "y", "z"], "present": [True] * 4}},
        {"data": {"values": [2], "present": [True]}},
    ]
    with pytest.raises(waevent.WAEventMismatch, match="out of range"):
        waevent.compare(emitted, expected, project=["data[3]"])


def test_a_projected_index_present_in_NO_event_is_caught_by_reachability():
    emitted = [{"data": {"values": [1, "x"], "present": [True, True]}}]
    expected = [{"data": {"values": [1, "x"], "present": [True, True]}}]
    with pytest.raises(waevent.WAEventMismatch, match="matched no EMITTED event"):
        waevent.compare(emitted, expected, project=["data[0]", "data[7]"])


def test_project_naming_no_array_section_scopes_that_section_out_entirely():
    # A projection over metadata only must not fail on a data section that differs -- including
    # one side omitting it, which is a presence difference at the SECTION level.
    emitted = [{"metadata": {"T": "1"}, "data": {"values": [9], "present": [True]}}]
    expected = [{"metadata": {"T": "1"}}]
    assert waevent.compare(emitted, expected, project=["metadata.T"]) is None


def test_a_plain_compare_still_catches_a_section_length_change():
    emitted = [{"data": {"values": [1, 2], "present": [True, True]}}]
    expected = [{"data": {"values": [1], "present": [True]}}]
    with pytest.raises(waevent.WAEventMismatch, match="length mismatch"):
        waevent.compare(emitted, expected)
