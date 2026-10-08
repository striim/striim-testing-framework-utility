import pytest
from livetest.assertions import AssertionFailed
from livetest.assertions.file import (
    parse_file_specs, parse_json_events, _project, _check_spec, assert_file, FileSpecError,
)
from livetest.striimfile import _check, StriimFileError

# ---- JSON event parsing (tolerant of the array wrapper / concatenation / partial) ----

_ARRAY = '[\n {"data":{"ID":"1","MSG":"a"},"userdata":null},\n {"data":{"ID":"2","MSG":"b"},"userdata":{"T":"x"}}\n]'

def test_parse_json_array():
    ev = parse_json_events(_ARRAY)
    assert [e["data"]["ID"] for e in ev] == ["1", "2"]

def test_parse_concatenated_arrays_from_rollover():
    ev = parse_json_events(_ARRAY + _ARRAY)     # two rolled files catted together
    assert len(ev) == 4

def test_parse_tolerates_unclosed_trailing():
    # file still being written: array not closed, last object partial
    text = '[\n {"data":{"ID":"1"}},\n {"data":{"ID":"2"}},\n {"data":{"ID'
    assert [e["data"]["ID"] for e in parse_json_events(text)] == ["1", "2"]

def test_parse_empty():
    assert parse_json_events("") == []

def test_parse_raises_on_mid_stream_corruption():
    # A well-formed event, then a corrupt token, then two MORE well-formed events: this
    # is not a trailing partial (there's decodable content after the failure point), so
    # it must raise rather than silently returning just the first event.
    text = '[{"data":{"ID":"1"}}, garbage, {"data":{"ID":"2"}}, {"data":{"ID":"3"}}]'
    with pytest.raises(FileSpecError, match="corrupt"):
        parse_json_events(text)

# ---- projection --------------------------------------------------------------

def test_project_data_userdata_all():
    e = {"data": {"ID": "1"}, "userdata": {"TIER": "gold"}}
    assert _project(e, "data") == {"ID": "1"}
    assert _project(e, "userdata") == {"TIER": "gold"}
    assert _project(e, "all") == {"ID": "1", "TIER": "gold"}

def test_project_handles_null_sections():
    assert _project({"data": None, "userdata": None}, "all") == {}

def test_project_includes_metadata_keys_nonnull():
    e = {"data": {"id": "1"}, "metadata": {"OperationName": "INSERT", "TableName": "public.customers"}}
    assert _project(e, "data", ["OperationName", "TableName"]) == {
        "id": "1", "OperationName": "INSERT", "TableName": "public.customers"}
    # a boundary event: null TableName is omitted -> only OperationName remains
    b = {"data": None, "metadata": {"OperationName": "BEGIN", "TableName": None}}
    assert _project(b, "data", ["OperationName", "TableName"]) == {"OperationName": "BEGIN"}

def test_project_all_excludes_before():
    # the before-image shares column names with `data` -- `all` must NOT fold it in,
    # else it would silently overwrite the current-image value with the pre-image.
    e = {"data": {"ID": "1", "AMT": "200"}, "before": {"ID": "1", "AMT": "100"}}
    assert _project(e, "all") == {"ID": "1", "AMT": "200"}

def test_project_before_mode():
    e = {"data": {"ID": "1", "AMT": "200"}, "before": {"ID": "1", "AMT": "100"}}
    assert _project(e, "before") == {"ID": "1", "AMT": "100"}

def test_project_keys_subset_ignores_other_columns():
    # keys restricts the projected dict to enrichment values, dropping runtime-
    # nondeterministic flags (e.g. Lookup's _cachehit/_dbqueried).
    e = {"data": {"ID": "1"},
         "userdata": {"NAME": "Record01", "L_cachehit": True, "L_dbqueried": False}}
    assert _project(e, "userdata", keys=["NAME"]) == {"NAME": "Record01"}
    assert _project(e, "all", keys=["ID", "NAME"]) == {"ID": "1", "NAME": "Record01"}
    # a listed-but-absent key simply doesn't appear (so a golden expecting it fails loudly)
    assert _project(e, "userdata", keys=["NAME", "MISSING"]) == {"NAME": "Record01"}

# ---- spec validation ---------------------------------------------------------

def test_parse_specs_ok():
    parse_file_specs([{"path": "/tmp/o", "match": "e.csv"}])
    parse_file_specs([{"path": "/tmp/o", "min_events": 3, "project": "all"}])
    parse_file_specs([{"path": "/tmp/o", "events": 3, "match": "e.csv"}])


@pytest.mark.parametrize("count", [0, -1, True, "3", 1.5, None])
def test_exact_events_requires_a_positive_integer(count):
    with pytest.raises(FileSpecError, match="positive integer"):
        parse_file_specs([{"path": "/tmp/o", "events": count}])


def test_exact_events_rejects_a_second_count_policy():
    with pytest.raises(FileSpecError, match="not both"):
        parse_file_specs([{"path": "/tmp/o", "events": 2, "min_events": 1}])


def test_exact_events_rejects_duplicates_that_a_row_set_would_hide():
    from livetest.assertions.file import _evaluate
    from livetest.assertions.data import distinct_set
    rows = [{"ID": "1"}, {"ID": "2"}]
    spec = {"path": "/tmp/o", "events": 2, "match": "e.csv", "keys": ["ID"]}
    assert _evaluate(lambda _: _ARRAY, spec, rows, distinct_set(rows))[0]
    read = lambda _: _ARRAY + '[{"data":{"ID":"2","MSG":"b"},"userdata":{"T":"x"}}]'
    ok, detail, expected, actual = _evaluate(read, spec, rows, distinct_set(rows))
    assert not ok
    assert "events=3, want events=2" in detail
    assert expected["count"] == 2 and actual["count"] == 3


def test_exact_events_records_the_matched_count():
    from livetest.assertions.file import _evaluate
    ok, detail, expected, actual = _evaluate(lambda _: _ARRAY, {"path": "/tmp/o", "events": 2}, None, None)
    assert ok and detail is None
    assert expected == actual == {"kind": "count", "count": 2}

def test_parse_specs_requires_path():
    with pytest.raises(FileSpecError):
        parse_file_specs([{"match": "e.csv"}])

def test_parse_specs_requires_match_or_min_events():
    with pytest.raises(FileSpecError):
        parse_file_specs([{"path": "/tmp/o"}])

def test_parse_specs_rejects_bad_projection():
    with pytest.raises(FileSpecError):
        parse_file_specs([{"path": "/tmp/o", "match": "e.csv", "project": "metadata"}])

def test_parse_specs_rejects_vacuous_min_events():
    with pytest.raises(FileSpecError, match="min_events"):
        parse_file_specs([{"path": "/tmp/o", "min_events": 0}])
    with pytest.raises(FileSpecError, match="min_events"):
        parse_file_specs([{"path": "/tmp/o", "min_events": -1}])

def test_parse_specs_rejects_non_numeric_min_events():
    with pytest.raises(FileSpecError):
        parse_file_specs([{"path": "/tmp/o", "min_events": "lots"}])

# ---- match / min_events against a golden ------------------------------------

def _golden(tmp_path):
    p = tmp_path / "e.csv"
    p.write_text("ID,MSG\n1,a\n2,b\n")
    return tmp_path

def test_check_spec_match_pass_and_fail(tmp_path):
    _golden(tmp_path)
    spec = {"path": "/tmp/o", "match": "e.csv", "project": "data"}
    from livetest.assertions.file import _load_golden
    _, want = _load_golden(tmp_path, spec)
    assert _check_spec(lambda p: _ARRAY, spec, want) is None          # matches
    short = '[{"data":{"ID":"1","MSG":"a"}}]'                          # missing row 2
    assert _check_spec(lambda p: short, spec, want) is not None

def test_load_golden_rows_renders_tokens(tmp_path):
    from livetest.assertions.file import _load_golden_rows
    p = tmp_path / "expected" / "x.csv"
    p.parent.mkdir()
    p.write_text("ID,MSG\n${TID},hi\n")
    rows = _load_golden_rows(tmp_path, {"match": "expected/x.csv"}, tokens={"TID": "t1"})
    assert rows == [{"ID": "t1", "MSG": "hi"}]

def test_check_spec_skips_metadata_only_boundary(tmp_path):
    # A boundary event (metadata but no data) must NOT count as a row even when a
    # metadata key is projected — else a BEGIN/COMMIT carrying source metadata leaks in.
    (tmp_path / "g.csv").write_text("ID,tbl\n1,t\n2,t\n")
    from livetest.assertions.file import _load_golden
    spec = {"path": "/tmp/o", "match": "g.csv", "project": "data", "metadata": ["tbl"]}
    _, want = _load_golden(tmp_path, spec)
    stream = ('[{"metadata":{"tbl":"t"},"data":null},'          # boundary -> skipped
              ' {"data":{"ID":"1"},"metadata":{"tbl":"t"}},'
              ' {"data":{"ID":"2"},"metadata":{"tbl":"t"}}]')
    assert _check_spec(lambda p: stream, spec, want) is None

def test_project_typed_event_uses_top_level_fields():
    from livetest.assertions.file import _project
    typed = {"id": 7, "copy": 2}
    assert _project(typed, "data") == typed
    assert _project(typed, "all", keys=["id"]) == {"id": 7}
    assert _project(typed, "userdata") == {}
    # A WAEvent with an empty data section is still a WAEvent, not a typed event.
    assert _project({"data": {}, "metadata": {"OperationName": "COMMIT"}}, "data") == {}

def test_parse_specs_distinct_events():
    parse_file_specs([{"path": "/tmp/o", "distinct_events": 2, "keys": ["ID"]}])
    for bad in (0, -1, True, "two"):
        with pytest.raises(FileSpecError, match="distinct_events"):
            parse_file_specs([{"path": "/tmp/o", "distinct_events": bad}])
    for other in ({"match": "e.csv"}, {"events": 2}, {"min_events": 1}):
        with pytest.raises(FileSpecError, match="stands alone"):
            parse_file_specs([{"path": "/tmp/o", "distinct_events": 2, **other}])

_TYPED = '[{"id":1,"copy":1,"pad":"x"},{"id":1,"copy":2,"pad":"x"},{"id":2,"copy":1,"pad":"y"}]'

def test_assert_file_distinct_events_typed_stream_with_keys(tmp_path):
    # The shape a recovery test uses: typed events, keys restricting the projection, the file
    # rolled twice (every event twice) -- 3 distinct (id, copy), not 6, and not 3 (id).
    spec = {"path": "/tmp/o", "distinct_events": 3, "keys": ["id", "copy"]}
    assert_file(lambda p: _TYPED + _TYPED, [spec], tmp_path, timeout=0)
    with pytest.raises(AssertionError, match="distinct projected events=2 \\(events=6\\)"):
        assert_file(lambda p: _TYPED + _TYPED, [{"path": "/tmp/o", "distinct_events": 3, "keys": ["id"]}], tmp_path, timeout=0)

def test_assert_file_distinct_events_skips_boundary_events(tmp_path):
    # A boundary event (metadata only, no payload) is not a row.
    text = '[{"data":{"ID":"1"}},{"metadata":{"OperationName":"COMMIT"}},{"data":{"ID":"1"}}]'
    assert_file(lambda p: text, [{"path": "/tmp/o", "distinct_events": 1}], tmp_path, timeout=0)

def test_assert_file_distinct_events_ignores_replayed_duplicates(tmp_path):
    # Two rolled files with the same two events: 4 events, 2 distinct rows.
    spec = {"path": "/tmp/o", "distinct_events": 2, "keys": ["ID"]}
    assert_file(lambda p: _ARRAY + _ARRAY, [spec], tmp_path, timeout=0)
    with pytest.raises(AssertionError, match="distinct projected events=2 \\(events=4\\)"):
        assert_file(lambda p: _ARRAY + _ARRAY, [{"path": "/tmp/o", "distinct_events": 3}], tmp_path, timeout=0)

def test_assert_file_min_events(tmp_path):
    spec = {"path": "/tmp/o", "min_events": 2}
    assert_file(lambda p: _ARRAY, [spec], tmp_path, timeout=0)         # 2 events -> ok
    with pytest.raises(AssertionError):
        assert_file(lambda p: '[{"data":{}}]', [spec], tmp_path, timeout=0)


# ---- structured per-assertion records -----------------------------------------------

def test_assert_file_min_events_success_returns_passed_record_with_count_snapshot(tmp_path):
    spec = {"path": "/tmp/o", "min_events": 2}
    records = assert_file(lambda p: _ARRAY, [spec], tmp_path, timeout=0, db="postgres")
    assert len(records) == 1
    rec = records[0]
    assert rec["status"] == "passed"
    assert rec["type"] == "file"
    assert rec["target"] == "/tmp/o"
    assert rec["db"] == "postgres"
    assert rec["spec"] == spec
    assert rec["expected"] == {"kind": "count", "count": 2, "truncated": False}
    assert rec["actual"] == {"kind": "count", "count": 2, "truncated": False}


def test_assert_file_match_success_returns_rows_snapshot(tmp_path):
    _golden(tmp_path)
    spec = {"path": "/tmp/o", "match": "e.csv", "project": "data"}
    records = assert_file(lambda p: _ARRAY, [spec], tmp_path, timeout=0)
    assert len(records) == 1
    assert records[0]["expected"]["kind"] == "rows"
    assert records[0]["actual"]["kind"] == "rows"
    assert records[0]["db"] is None


def test_assert_file_failure_raises_assertion_failed_with_records(tmp_path):
    spec = {"path": "/tmp/o", "min_events": 5}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_file(lambda p: _ARRAY, [spec], tmp_path, timeout=0)   # only 2 events
    assert str(exc_info.value) == (
        "file assertion not satisfied within 0s:\n"
        "/tmp/o: events=2, want min_events>=5\n"
        "\n"
        "  EXPECTED: 5 events\n"
        "  ACTUAL: 2 events"
    )
    records = exc_info.value.records
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["expected"] == {"kind": "count", "count": 5, "truncated": False}
    assert records[0]["actual"] == {"kind": "count", "count": 2, "truncated": False}


def test_assert_file_mixed_specs_report_both_statuses(tmp_path):
    ok_spec = {"path": "/tmp/ok", "min_events": 1}
    bad_spec = {"path": "/tmp/bad", "min_events": 5}
    with pytest.raises(AssertionFailed) as exc_info:
        assert_file(lambda p: _ARRAY, [ok_spec, bad_spec], tmp_path, timeout=0)
    records = exc_info.value.records
    assert len(records) == 2
    by_target = {r["target"]: r for r in records}
    assert by_target["/tmp/ok"]["status"] == "passed"
    assert by_target["/tmp/bad"]["status"] == "failed"

# ---- server-file path safety -------------------------------------------------

def test_check_rejects_shell_metachars():
    with pytest.raises(StriimFileError):
        _check("/tmp/out; rm -rf /")
    assert _check("/tmp/SLT_x-filewriter") == "/tmp/SLT_x-filewriter"

# ---- server_files manifest parsing ------------------------------------------

def test_server_files_parse_ok():
    from livetest.manifest import _normalize_server_files
    got = _normalize_server_files([
        {"file": "input.csv", "dest": "/tmp/${NS}-in/input.csv"},
        {"file": "b.csv", "dest": "/tmp/x/b.csv", "when": "post_start"},
        {"file": "c.jar", "dest": "${TID}c.jar", "load": True},
    ])
    assert got == [("input.csv", "/tmp/${NS}-in/input.csv", "pre_deploy", False),
                   ("b.csv", "/tmp/x/b.csv", "post_start", False),
                   ("c.jar", "${TID}c.jar", "pre_deploy", "udf")]
    assert _normalize_server_files(None) == []

def test_server_files_requires_file_and_dest():
    from livetest.manifest import _normalize_server_files, ManifestError
    with pytest.raises(ManifestError):
        _normalize_server_files([{"file": "input.csv"}])          # no dest
    with pytest.raises(ManifestError):
        _normalize_server_files([{"dest": "/tmp/x"}])             # no file

def test_server_files_bad_when():
    from livetest.manifest import _normalize_server_files, ManifestError
    with pytest.raises(ManifestError):
        _normalize_server_files([{"file": "a", "dest": "/tmp/a", "when": "later"}])


# --------------------------------------------------------------------------------
# Failure output must show what was COMPARED, not the raw rows.
# --------------------------------------------------------------------------------

def test_failure_output_shows_normalized_values_not_raw_ones():
    """The display defect that cost three weeks on a quarantined test.

    A parsed JSON event holds a real bool; a golden CSV holds text. Printing both raw put
    `True` beside `'True'`, which reads as a TYPE mismatch -- so the operator was blamed. The
    comparison had actually seen `'true'` vs `'True'`: a one-character SPELLING mismatch in the
    golden. Showing the compared form makes the real difference legible.
    """
    from livetest.assertions.file import _format_failure_detail

    expected_rows = [{"ID": "2", "EX_parsed": "True"}]     # golden CSV: text, wrong spelling
    actual_rows = [{"ID": "2", "EX_parsed": True}]         # parsed JSON: a real bool
    results = [(False, "mismatch",
                {"kind": "rows", "rows": expected_rows},
                {"kind": "rows", "rows": actual_rows})]

    text = _format_failure_detail(results)

    # The bool is rendered the way the comparison renders it.
    assert "'EX_parsed': 'true'" in text, text
    # And never as a bare Python bool, which is what made it look like a type problem.
    assert "'EX_parsed': True" not in text, text
    # The golden's wrong spelling is visible next to it, so the diff is one character.
    assert "'EX_parsed': 'True'" in text, text


def test_normalized_row_matches_what_distinct_set_compares():
    """`normalized_row` is only useful if it cannot drift from the comparison."""
    from livetest.assertions.data import distinct_set, normalized_row

    row = {"a": True, "b": False, "c": 3, "d": "x", "e": None}

    assert frozenset(normalized_row(row).items()) in distinct_set([row])
    assert normalized_row(row) == {"a": "true", "b": "false", "c": "3", "d": "x", "e": None}


# ---- server_files input-dir cleanup -----------------------------------------
# A FileWriter's output is cleared between runs because it appends. A FileReader tailing a
# directory has the same problem in reverse and it is worse: it re-reads whatever it finds,
# including a fixture that has since been renamed, so the golden silently gains rows.

def _native_ctx():
    from types import SimpleNamespace
    return SimpleNamespace(mode="native")


def test_clear_server_dir_empties_the_directory(tmp_path):
    from livetest.striimfile import clear_server_dir
    d = tmp_path / "SLT_x-in"
    d.mkdir()
    (d / "baseline.csv").write_text("id,msg\n1,a\n")
    (d / "leftover-from-a-previous-run.csv").write_text("id,msg\n99,stale\n")

    clear_server_dir(_native_ctx(), str(d / "baseline.csv"))

    assert list(d.iterdir()) == [], "every file in the reader's directory must go, not just the one named"
    assert d.is_dir(), "the directory itself must survive — a FileReader watching it starts before the drop"


def test_clear_server_dir_leaves_nested_directories(tmp_path):
    # Non-recursive on purpose: a nested dir is not this test's input, and rm -rf on a
    # server path is not something a cleanup step should do implicitly.
    from livetest.striimfile import clear_server_dir
    d = tmp_path / "SLT_y-in"
    (d / "keep").mkdir(parents=True)
    (d / "gone.csv").write_text("x\n")

    clear_server_dir(_native_ctx(), str(d / "gone.csv"))

    assert (d / "keep").is_dir()
    assert not (d / "gone.csv").exists()


def test_clear_server_dir_rejects_an_unsafe_path():
    from livetest.striimfile import clear_server_dir, StriimFileError
    with pytest.raises(StriimFileError):
        clear_server_dir(_native_ctx(), "/tmp/$(rm -rf /)/x.csv")


# ---- the ABSENT marker: one golden covering records of DIFFERENT shapes ------
#
# `_project` keeps only the keys a record carries, so an unenriched record projects to
# fewer keys than an enriched one. A CSV golden is rectangular and DictReader yields ''
# for a blank cell, so before ABSENT existed there was no way to write a golden asserting
# a value column on one record and its absence on another. That is not a hypothetical:
# it is why lookup-write-through-constraints could not ship an order on a
# constraint-rejected product without dropping its value assertions (EC_STATUS 8.107).

def test_null_marker_becomes_a_REAL_none_in_the_expected_row():
    from livetest.assertions.data import apply_null, NULL
    rows = apply_null([{"COUNTRY": "FR", "REGION": NULL}])
    assert rows == [{"COUNTRY": "FR", "REGION": None}], "the marked cell must be None, not text"


def test_null_matches_a_record_that_CARRIES_the_key_with_a_null_value():
    # The gap 8.128 names. A record whose REGION is null keeps the KEY -- `_project` drops
    # only keys a record lacks -- so <absent> cannot match it, and an empty cell asserts the
    # empty STRING. Measured on a live run of csv-format-options, whose nullToken makes
    # REGION null for exactly one row: both spellings were tried and both failed.
    from livetest.assertions.data import apply_null, distinct_set, NULL
    golden = apply_null([
        {"COUNTRY": "DE", "REGION": "EMEA"},
        {"COUNTRY": "FR", "REGION": NULL},
    ])
    actual = [{"COUNTRY": "DE", "REGION": "EMEA"}, {"COUNTRY": "FR", "REGION": None}]
    assert distinct_set(golden) == distinct_set(actual)


def test_null_is_NOT_absent_and_NOT_an_empty_cell():
    # Three distinct states, three spellings. Collapsing any pair is what made this a finding
    # rather than a typo: <absent> says the key is missing, '' says it is present and empty,
    # <null> says it is present and null.
    from livetest.assertions.data import apply_null, distinct_set, strip_absent, ABSENT, NULL
    as_null = apply_null([{"K": NULL}])
    as_empty = [{"K": ""}]
    as_absent = strip_absent([{"K": ABSENT}])
    assert distinct_set(as_null) != distinct_set(as_empty), "null must not equal empty"
    assert distinct_set(as_null) != distinct_set(as_absent), "null must not equal absent"
    assert distinct_set(as_empty) != distinct_set(as_absent), "empty must not equal absent"


def test_null_is_a_RESERVED_value_a_golden_cannot_escape():
    # Same limitation <absent> carries, stated rather than discovered: a record whose value is
    # literally the text "<null>" cannot be asserted, because the marker is applied to the
    # GOLDEN and there is no escape spelling. Nothing in the corpus writes it.
    from livetest.assertions.data import apply_null, NULL
    assert apply_null([{"K": NULL}]) == [{"K": None}]


def test_assert_data_ACCEPTS_the_null_marker_where_it_refuses_absent():
    # The asymmetry is deliberate and the refusal message now points here. A SELECT always
    # returns every column, so <absent> can never match -- but it returns NULL as None, which
    # is exactly what <null> produces. Before this the refusal said "assert the NULL directly"
    # and offered no way to write one.
    import io, csv as _csv
    from pathlib import Path as _P
    from livetest.assertions import data as d
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = _P(td) / "g.csv"
        f.write_text("ID,NAME\n1,<null>\n")
        assert d.load_golden(f) == [{"ID": "1", "NAME": None}]


def test_the_null_marker_is_APPLIED_when_a_golden_FILE_is_loaded():
    # The wiring, not the helper. Every other test here calls apply_null itself, so removing
    # the call from `_load_golden_rows` -- the file path this finding is about -- survived
    # them all. Measured: that mutant passed 48 tests. This one reads a real golden file.
    import tempfile
    from pathlib import Path as _P
    from livetest.assertions.file import _load_golden_rows
    with tempfile.TemporaryDirectory() as td:
        (_P(td) / "expected").mkdir()
        (_P(td) / "expected" / "g.csv").write_text("COUNTRY,REGION\nDE,EMEA\nFR,<null>\n")
        rows = _load_golden_rows(td, {"path": "/tmp/out", "match": "expected/g.csv"})
    assert rows == [{"COUNTRY": "DE", "REGION": "EMEA"}, {"COUNTRY": "FR", "REGION": None}], rows


def test_absent_marker_drops_the_key_from_the_expected_row():
    from livetest.assertions.data import strip_absent, ABSENT
    rows = strip_absent([{"ORDER_ID": "5005", "PRODUCT_NAME": ABSENT}])
    assert rows == [{"ORDER_ID": "5005"}], "the marked key must be gone, not empty"


def test_absent_matches_a_record_that_lacks_the_key():
    from livetest.assertions.data import distinct_set, strip_absent, ABSENT
    golden = strip_absent([
        {"ORDER_ID": "5001", "PRODUCT_NAME": "Widget Pro"},
        {"ORDER_ID": "5005", "PRODUCT_NAME": ABSENT},
    ])
    actual = [{"ORDER_ID": 5001, "PRODUCT_NAME": "Widget Pro"}, {"ORDER_ID": 5005}]
    assert distinct_set(golden) == distinct_set(actual)


def test_absent_is_NOT_the_same_as_an_empty_cell():
    # The whole point of the marker. A blank cell keeps the key with value '', which does
    # NOT match a record missing that key -- if these two ever converge, a golden stops
    # being able to tell "no value column" from "empty value column".
    from livetest.assertions.data import distinct_set, strip_absent, ABSENT
    absent = strip_absent([{"ORDER_ID": "5005", "PRODUCT_NAME": ABSENT}])
    blank = strip_absent([{"ORDER_ID": "5005", "PRODUCT_NAME": ""}])
    assert distinct_set(absent) != distinct_set(blank)
    assert distinct_set(absent) == distinct_set([{"ORDER_ID": 5005}])


def test_absent_is_a_RESERVED_value_a_golden_cannot_escape():
    # strip_absent touches GOLDEN rows only, so nothing rewrites the event -- but the
    # consequence is that a record whose value is literally "<absent>" can never be MATCHED,
    # because every golden cell reading "<absent>" is stripped. That is a reserved value, not
    # a safety property, and this test pins which of the two it is.
    from livetest.assertions.data import distinct_set, strip_absent, ABSENT
    actual = [{"ORDER_ID": 5005, "PRODUCT_NAME": ABSENT}]
    assert distinct_set(strip_absent([{"ORDER_ID": "5005", "PRODUCT_NAME": ABSENT}])) \
        != distinct_set(actual)


def test_absent_end_to_end_through_a_file_spec():
    # The path a real case takes: two events of different shapes, one golden.
    text = ('[{"data":{"ORDER_ID":"5001"},"userdata":{"PRODUCT_NAME":"Widget Pro"}},'
            ' {"data":{"ORDER_ID":"5005"},"userdata":{"ProductLookup_cachehit":false}}]')
    from livetest.assertions.data import distinct_set, strip_absent, ABSENT
    spec = {"path": "/tmp/out", "project": "all", "keys": ["ORDER_ID", "PRODUCT_NAME"],
            "match": "expected/enriched.csv"}
    want = distinct_set(strip_absent([
        {"ORDER_ID": "5001", "PRODUCT_NAME": "Widget Pro"},
        {"ORDER_ID": "5005", "PRODUCT_NAME": ABSENT},
    ]))
    assert _check_spec(lambda p: text, spec, want) is None

    # negative control: claiming a value where the record has none must FAIL
    wrong = distinct_set(strip_absent([
        {"ORDER_ID": "5001", "PRODUCT_NAME": "Widget Pro"},
        {"ORDER_ID": "5005", "PRODUCT_NAME": "Legacy Item"},
    ]))
    assert _check_spec(lambda p: text, spec, wrong) is not None


def test_an_all_absent_golden_is_REFUSED_as_vacuous():
    # The <absent> marker made a new vacuous shape reachable: a golden whose every row
    # strips to nothing is not EMPTY, so the empty-golden guard cannot see it, but it
    # asserts only "some event carries none of the keys" -- which an unenriched record
    # satisfies. Such a spec would stay green through a TOTAL enrichment failure.
    import pytest as _pytest, pathlib, tempfile
    from livetest.assertions.file import _load_golden, FileSpecError
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "expected").mkdir()
        (pathlib.Path(d) / "expected" / "g.csv").write_text("A,B\n<absent>,<absent>\n")
        spec = {"path": "/tmp/out", "keys": ["A", "B"], "match": "expected/g.csv"}
        with _pytest.raises(FileSpecError, match="strips to nothing"):
            _load_golden(d, spec)


def test_an_absent_row_ALONGSIDE_a_real_row_is_still_accepted():
    # Negative control: the guard must refuse only the vacuous shape. Every golden this
    # repository actually ships pairs its <absent> row with real ones.
    import pathlib, tempfile
    from livetest.assertions.file import _load_golden
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "expected").mkdir()
        (pathlib.Path(d) / "expected" / "g.csv").write_text("A,B\n1,x\n<absent>,<absent>\n")
        spec = {"path": "/tmp/out", "keys": ["A", "B"], "match": "expected/g.csv"}
        rows, want = _load_golden(d, spec)
        assert len(want) == 2 and frozenset() in want


def test_an_absent_row_with_NO_keys_is_REFUSED_as_impossible():
    # Neighbour of the vacuous shape, and a different problem. `_evaluate` keeps events by
    # the UNRESTRICTED projection, so without `keys:` every compared event projects at least
    # one column and frozenset() can never appear in the actual set. Such a golden is
    # IMPOSSIBLE rather than vacuous, and would fail only after the full assertion timeout
    # with a diff naming no cause -- so it is refused at load, saying which of the two it is.
    import pytest as _pytest, pathlib, tempfile
    from livetest.assertions.file import _load_golden, FileSpecError
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "expected").mkdir()
        (pathlib.Path(d) / "expected" / "g.csv").write_text("A,B\n1,x\n<absent>,<absent>\n")
        spec = {"path": "/tmp/out", "match": "expected/g.csv"}      # no keys:
        with _pytest.raises(FileSpecError, match="no `keys:`"):
            _load_golden(d, spec)


def test_the_same_golden_WITH_keys_is_accepted():
    # Negative control: it is the missing `keys:` that makes it impossible, not the marker.
    import pathlib, tempfile
    from livetest.assertions.file import _load_golden
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "expected").mkdir()
        (pathlib.Path(d) / "expected" / "g.csv").write_text("A,B\n1,x\n<absent>,<absent>\n")
        rows, want = _load_golden(d, {"path": "/tmp/out", "keys": ["A", "B"], "match": "expected/g.csv"})
        assert frozenset() in want and len(want) == 2


# ---- ordering / multiplicity / stability ----------------------------------------------

def _events(*rows):
    import json as _json
    return _json.dumps([{"data": r} for r in rows])


def _golden_rows(tmp_path, name, header, *rows):
    (tmp_path / name).write_text(header + "\n" + "\n".join(rows) + "\n")


def test_har01_multiset_detects_a_duplicate_the_distinct_set_accepts(tmp_path):
    _golden_rows(tmp_path, "g.csv", "ID", "1", "2")
    text = _events({"ID": "1"}, {"ID": "2"}, {"ID": "2"})
    distinct = {"path": "/o", "match": "g.csv"}
    assert_file(lambda p: text, [distinct], tmp_path, timeout=0)       # the old compare passes
    with pytest.raises(AssertionFailed, match="multiset differs"):
        assert_file(lambda p: text, [dict(distinct, multiset=True)], tmp_path, timeout=0)


def test_har02_multiset_detects_missing_plus_duplicate_at_equal_count(tmp_path):
    _golden_rows(tmp_path, "g.csv", "ID", "1", "2", "3")
    text = _events({"ID": "1"}, {"ID": "2"}, {"ID": "2"})               # 3 missing, 2 twice
    spec = {"path": "/o", "match": "g.csv", "multiset": True, "events": 3}
    with pytest.raises(AssertionFailed):
        assert_file(lambda p: text, [spec], tmp_path, timeout=0)


def test_har02_multiset_passes_on_exact_multiplicity(tmp_path):
    _golden_rows(tmp_path, "g.csv", "ID", "1", "2", "2")
    text = _events({"ID": "2"}, {"ID": "1"}, {"ID": "2"})
    assert_file(lambda p: text, [{"path": "/o", "match": "g.csv", "multiset": True}],
                tmp_path, timeout=0)


_EDGE = [{"before": {"T": "P", "ID": "200"}, "after": {"T": "C", "PID": "200"}}]


def test_har03_order_fails_when_child_precedes_parent_with_the_same_set(tmp_path):
    good = _events({"T": "P", "ID": "200"}, {"T": "C", "PID": "200", "ID": "3"})
    bad = _events({"T": "C", "PID": "200", "ID": "3"}, {"T": "P", "ID": "200"})
    spec = {"path": "/o", "min_events": 2, "order": _EDGE}
    assert_file(lambda p: good, [spec], tmp_path, timeout=0)
    with pytest.raises(AssertionFailed, match="precedes"):
        assert_file(lambda p: bad, [spec], tmp_path, timeout=0)


def test_har03_order_accepts_unrelated_interleaving(tmp_path):
    text = _events({"T": "C", "PID": "999", "ID": "7"}, {"T": "P", "ID": "200"},
                   {"T": "P", "ID": "400"}, {"T": "C", "PID": "200", "ID": "3"})
    assert_file(lambda p: text, [{"path": "/o", "min_events": 1, "order": _EDGE}], tmp_path,
                timeout=0)


def test_har03_order_edge_cannot_pass_vacuously(tmp_path):
    text = _events({"T": "P", "ID": "200"})                               # no child at all
    with pytest.raises(AssertionFailed, match="no event matches after"):
        assert_file(lambda p: text, [{"path": "/o", "min_events": 1, "order": _EDGE}],
                    tmp_path, timeout=0)


def test_har04_stable_window_fails_on_a_late_extra_event(tmp_path):
    _golden_rows(tmp_path, "g.csv", "ID", "1")
    reads = {"n": 0}

    def read(_):
        reads["n"] += 1
        return _events({"ID": "1"}) if reads["n"] < 3 else _events({"ID": "1"}, {"ID": "X"})

    spec = {"path": "/o", "match": "g.csv", "stable_seconds": 1}
    with pytest.raises(AssertionFailed, match="stable_seconds"):
        assert_file(read, [spec], tmp_path, timeout=5, poll=0.01)


def test_har04_stable_window_passes_when_nothing_changes(tmp_path):
    _golden_rows(tmp_path, "g.csv", "ID", "1")
    spec = {"path": "/o", "match": "g.csv", "stable_seconds": 0.2}
    assert_file(lambda p: _events({"ID": "1"}), [spec], tmp_path, timeout=5, poll=0.01)


@pytest.mark.parametrize("bad", [
    {"path": "/o", "min_events": 1, "multiset": True},                  # multiset without golden
    {"path": "/o", "min_events": 1, "multiset": "yes"},
    {"path": "/o", "min_events": 1, "order": []},
    {"path": "/o", "min_events": 1, "order": [{"before": {"A": "1"}}]},
    {"path": "/o", "min_events": 1, "order": [{"before": {}, "after": {"A": "1"}}]},
    {"path": "/o", "min_events": 1, "stable_seconds": -1},
    {"path": "/o", "min_events": 1, "stable_seconds": True},
])
def test_ordering_spec_validation(bad):
    with pytest.raises(FileSpecError):
        parse_file_specs([bad])
