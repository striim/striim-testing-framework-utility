"""inttest.monitorcheck: assert.monitor's subset comparison for JSON metrics (ADAPTER_BASIC)."""

from inttest.monitorcheck import render_strings, subset_mismatches

PUBLISHED = {
    "Tuning": {"Windows": 2, "CommitMsP95": 41, "ClosedOnEventCount": 1,
               "IgnoredByCode": {"UNSUPPORTED_EVENT": 1}, "IdlePct": 97.5},
    "Debug": {"WindowInDoubt": False, "CheckpointPosition": "x"},
}


def test_named_keys_that_match_pass_and_unnamed_keys_are_ignored():
    assert subset_mismatches({"Tuning": {"Windows": 2, "IgnoredByCode": {"UNSUPPORTED_EVENT": 1}},
                              "Debug": {"WindowInDoubt": False}}, PUBLISHED) == []


def test_a_wrong_value_names_its_path():
    assert subset_mismatches({"Tuning": {"Windows": 3}}, PUBLISHED) == [
        "Tuning.Windows: expected 3, published 2"]


def test_a_missing_key_names_its_path_and_the_keys_there():
    out = subset_mismatches({"Tuning": {"Nope": 1}}, PUBLISHED)
    assert len(out) == 1 and out[0].startswith("Tuning.Nope: missing")


def test_an_object_expected_where_a_scalar_is_published():
    assert subset_mismatches({"Debug": {"CheckpointPosition": {"a": 1}}}, PUBLISHED) == [
        "Debug.CheckpointPosition: expected an object, published 'x'"]


def test_nothing_published_fails_every_named_key():
    assert subset_mismatches({"Tuning": {"Windows": 2}}, None) == [
        "<root>: expected an object, published None"]


def test_true_is_not_one():
    assert subset_mismatches({"a": 1}, {"a": True}) != []
    assert subset_mismatches({"a": True}, {"a": 1}) != []
    assert subset_mismatches({"a": 2.0}, {"a": 2}) == []


def test_render_strings_renders_leaves_only():
    out = render_strings({"${K}": "${V}", "n": 1, "l": ["${V}"]}, lambda s: s.replace("${V}", "v"))
    assert out == {"${K}": "v", "n": 1, "l": ["v"]}
