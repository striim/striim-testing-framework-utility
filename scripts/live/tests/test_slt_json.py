import json

import pytest

from livetest.resultschema import (
    SCHEMA_VERSION,
    SchemaError,
    build_assertion_result,
    truncate,
    validate,
    write_sidecar,
)


def _rows_assertion(status="passed", n=1):
    return build_assertion_result(
        type="data",
        status=status,
        spec={"target": "slt_x.enriched", "match": "expected/enriched.csv"},
        target="slt_x.enriched",
        db="postgres",
        detail="ok" if status == "passed" else "mismatch",
        expected={"kind": "rows", "rows": [{"id": str(i)} for i in range(n)]},
        actual={"kind": "rows", "rows": [{"id": str(i)} for i in range(n)]},
    )


def _well_formed_doc():
    smoke = build_assertion_result(
        type="smoke", status="passed", spec={}, target=None, db=None, detail="RUNNING",
    )
    data_pass = _rows_assertion(status="passed")
    diff_fail = build_assertion_result(
        type="diff",
        status="failed",
        spec={"source": "slt_x.src", "target": "slt_x.tgt"},
        target="slt_x.tgt",
        db="postgres",
        detail="target has not caught up",
        expected={"kind": "count", "count": 3},
        actual={"kind": "count", "count": 1},
    )
    file_pass = build_assertion_result(
        type="file",
        status="passed",
        spec={"path": "/data/out.json", "match": "expected/out.csv"},
        target="/data/out.json",
        db=None,
        detail="ok",
        expected={"kind": "rows", "rows": [{"a": "1"}]},
        actual={"kind": "rows", "rows": [{"a": "1"}]},
    )
    gcs_fail = build_assertion_result(
        type="gcs",
        status="failed",
        spec={"bucket": "b", "object": "o", "sha256": "deadbeef"},
        target="b/o",
        db=None,
        detail="sha256 mismatch",
        expected={"kind": "bytes", "size": 128},
        actual={"kind": "bytes", "size": 64},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "tests": [
            {
                "name": "lookup-composite-key",
                "nodeid": "regression/op/lookup/lookup-composite-key/test.yaml",
                "status": "passed",
                "topology": "single",
                "services": ["postgres"],
                "duration": 12.3,
                "skip_reason": None,
                "assertions": [smoke, data_pass],
            },
            {
                "name": "debezium-diff-and-file",
                "nodeid": "regression/op/debezium/diff-and-file/test.yaml",
                "status": "failed",
                "topology": "cluster",
                "services": ["postgres", "gcs"],
                "duration": 5.0,
                "skip_reason": None,
                "assertions": [diff_fail, file_pass, gcs_fail],
            },
            {
                "name": "skipped-example",
                "nodeid": "regression/op/skipped/test.yaml",
                "status": "skipped",
                "topology": "single",
                "services": [],
                "duration": 0.0,
                "skip_reason": "SLT_KAFKA not set",
                "assertions": [],
            },
        ],
    }


# --------------------------------------------------------------------------------------
# validate() — accepts well-formed
# --------------------------------------------------------------------------------------


def test_validate_accepts_well_formed_doc_covering_all_assertion_types():
    doc = _well_formed_doc()
    validate(doc)  # must not raise


# --------------------------------------------------------------------------------------
# validate() — rejects
# --------------------------------------------------------------------------------------


def test_validate_rejects_missing_schema_version():
    doc = _well_formed_doc()
    del doc["schema_version"]
    with pytest.raises(SchemaError, match="schema_version"):
        validate(doc)


def test_validate_rejects_wrong_schema_version():
    doc = _well_formed_doc()
    doc["schema_version"] = 2
    with pytest.raises(SchemaError, match="schema_version"):
        validate(doc)


def test_validate_rejects_unknown_status_enum():
    doc = _well_formed_doc()
    doc["tests"][0]["status"] = "borked"
    with pytest.raises(SchemaError, match="status"):
        validate(doc)


def test_validate_rejects_unknown_assertion_status_enum():
    doc = _well_formed_doc()
    doc["tests"][0]["assertions"][0]["status"] = "borked"
    with pytest.raises(SchemaError, match="status"):
        validate(doc)


def test_validate_rejects_unknown_topology():
    doc = _well_formed_doc()
    doc["tests"][0]["topology"] = "quorum"
    with pytest.raises(SchemaError, match="topology"):
        validate(doc)


def test_validate_rejects_unknown_assertion_type():
    doc = _well_formed_doc()
    doc["tests"][0]["assertions"][0]["type"] = "explode"
    with pytest.raises(SchemaError, match="type"):
        validate(doc)


def test_validate_rejects_missing_required_assertion_field():
    doc = _well_formed_doc()
    del doc["tests"][0]["assertions"][0]["detail"]
    with pytest.raises(SchemaError, match="detail"):
        validate(doc)


def test_validate_rejects_missing_required_test_field():
    doc = _well_formed_doc()
    del doc["tests"][0]["duration"]
    with pytest.raises(SchemaError, match="duration"):
        validate(doc)


def test_validate_rejects_extra_unknown_key():
    doc = _well_formed_doc()
    doc["tests"][0]["bogus"] = 1
    with pytest.raises(SchemaError, match="bogus"):
        validate(doc)


def test_validate_rejects_wrong_type_for_duration():
    doc = _well_formed_doc()
    doc["tests"][0]["duration"] = "fast"
    with pytest.raises(SchemaError, match="duration"):
        validate(doc)


def test_validate_rejects_wrong_type_for_services():
    doc = _well_formed_doc()
    doc["tests"][0]["services"] = "postgres"
    with pytest.raises(SchemaError, match="services"):
        validate(doc)


def test_validate_rejects_bad_kind():
    doc = _well_formed_doc()
    doc["tests"][0]["assertions"][1]["expected"]["kind"] = "banana"
    with pytest.raises(SchemaError, match="kind"):
        validate(doc)


def test_validate_rejects_rows_kind_missing_rows_key():
    doc = _well_formed_doc()
    del doc["tests"][0]["assertions"][1]["expected"]["rows"]
    with pytest.raises(SchemaError, match="rows"):
        validate(doc)


def test_validate_rejects_count_kind_with_rows_present():
    doc = _well_formed_doc()
    diff_expected = doc["tests"][1]["assertions"][0]["expected"]
    assert diff_expected["kind"] == "count"
    diff_expected["rows"] = [{"a": "1"}]
    with pytest.raises(SchemaError, match="rows"):
        validate(doc)


def test_validate_rejects_non_dict_doc():
    with pytest.raises(SchemaError):
        validate([1, 2, 3])


def test_validate_rejects_non_bool_truncated():
    doc = _well_formed_doc()
    doc["tests"][0]["assertions"][1]["expected"]["truncated"] = "yes"
    with pytest.raises(SchemaError, match="truncated"):
        validate(doc)


# --------------------------------------------------------------------------------------
# truncate()
# --------------------------------------------------------------------------------------


def test_truncate_under_limits_returns_unchanged_and_flag_false():
    rows = [{"id": str(i)} for i in range(5)]
    kept, truncated = truncate(rows, max_rows=200, max_bytes=65536)
    assert kept == rows
    assert truncated is False


def test_truncate_by_row_count():
    rows = [{"id": str(i)} for i in range(10)]
    kept, truncated = truncate(rows, max_rows=3, max_bytes=65536)
    assert kept == rows[:3]
    assert truncated is True


def test_truncate_by_byte_size():
    # Each row ~ {"id": "<50 chars>"} — big enough that only a few fit in a small budget.
    rows = [{"id": "x" * 50, "n": i} for i in range(50)]
    kept, truncated = truncate(rows, max_rows=200, max_bytes=500)
    assert len(kept) < len(rows)
    assert truncated is True
    # sanity: what's kept really does fit close to the budget (allow the "always keep
    # at least one row" escape hatch to slightly exceed it only when a single row alone
    # is oversized, which is not the case here).
    assert len(json.dumps(kept).encode("utf-8")) <= 500 or len(kept) == 1


def test_truncate_empty_rows():
    kept, truncated = truncate([], max_rows=200, max_bytes=65536)
    assert kept == []
    assert truncated is False


def test_truncate_keeps_at_least_one_oversized_row():
    huge_row = {"blob": "y" * 10_000}
    kept, truncated = truncate([huge_row], max_rows=200, max_bytes=100)
    assert kept == [huge_row]
    assert truncated is False  # nothing was dropped -- the single row was just huge


# --------------------------------------------------------------------------------------
# write_sidecar()
# --------------------------------------------------------------------------------------


def test_write_sidecar_round_trips(tmp_path):
    doc = _well_formed_doc()
    path = tmp_path / "results" / "live-example.slt.json"
    write_sidecar(path, doc["tests"])

    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert on_disk["tests"] == doc["tests"]
    validate(on_disk)  # must not raise


def test_write_sidecar_rejects_malformed_before_writing(tmp_path):
    path = tmp_path / "live-bad.slt.json"
    bad_tests = [{"name": "x"}]  # missing every other required TestResult field
    with pytest.raises(SchemaError):
        write_sidecar(path, bad_tests)
    assert not path.exists()


# --------------------------------------------------------------------------------------
# build_assertion_result()
# --------------------------------------------------------------------------------------


def test_build_assertion_result_applies_truncation_and_validates():
    rows = [{"id": str(i)} for i in range(10)]
    result = build_assertion_result(
        type="data",
        status="failed",
        spec={"target": "slt_x.t", "match": "expected/t.csv"},
        target="slt_x.t",
        db="postgres",
        detail="mismatch",
        expected={"kind": "rows", "rows": rows},
        actual={"kind": "rows", "rows": rows},
        max_rows=3,
        max_bytes=65536,
    )
    assert result["expected"]["rows"] == rows[:3]
    assert result["expected"]["truncated"] is True
    assert result["expected"]["count"] == 10
    assert result["actual"]["truncated"] is True

    # A single AssertionResult validates on its own when embedded in a minimal doc.
    doc = {
        "schema_version": SCHEMA_VERSION,
        "tests": [{
            "name": "t", "nodeid": "n", "status": "failed", "topology": "single",
            "services": [], "duration": 1.0, "skip_reason": None, "assertions": [result],
        }],
    }
    validate(doc)


def test_build_assertion_result_smoke_has_no_row_data():
    result = build_assertion_result(
        type="smoke", status="passed", spec={}, detail="RUNNING",
    )
    assert result["expected"] is None
    assert result["actual"] is None
    doc = {
        "schema_version": SCHEMA_VERSION,
        "tests": [{
            "name": "t", "nodeid": "n", "status": "passed", "topology": "single",
            "services": [], "duration": 1.0, "skip_reason": None, "assertions": [result],
        }],
    }
    validate(doc)


def test_build_assertion_result_rejects_bad_status():
    with pytest.raises(SchemaError, match="status"):
        build_assertion_result(type="data", status="borked", spec={}, detail="x")
