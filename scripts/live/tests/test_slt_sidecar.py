"""Hermetic tests for the plugin's .slt.json sidecar collector + writer.

These drive the collection/writer machinery directly with SimpleNamespace fakes — no Striim
cluster, no real deploy, no pytester — so they stay fast and hermetic. The full deploy path in
LiveItem.runtest is exercised elsewhere; here we prove that given per-item records + reports, the
plugin assembles a schema-valid sidecar covering pass / fail / skip / truncation.
"""
from __future__ import annotations

import json
import types

from livetest.plugin import _slt_sidecar_path, _slt_collect_report, _slt_write_sidecar
from livetest.resultschema import build_assertion_result, validate


def _config(xmlpath=None):
    return types.SimpleNamespace(option=types.SimpleNamespace(xmlpath=xmlpath))


def _item(nodeid, name, records, topology="single", services=None):
    return types.SimpleNamespace(
        nodeid=nodeid, name=name,
        _slt_records=records, _slt_topology=topology,
        _slt_services=services if services is not None else [],
        user_properties=[],
    )


def _report(when, outcome, duration=1.0, longrepr=None):
    return types.SimpleNamespace(when=when, outcome=outcome, duration=duration, longrepr=longrepr)


def _data_record(status="passed", n=1):
    return build_assertion_result(
        type="data", status=status,
        spec={"target": "slt_x.t", "match": "expected/t.csv"},
        target="slt_x.t", db="postgres",
        detail="ok" if status == "passed" else "mismatch",
        expected={"kind": "rows", "rows": [{"id": str(i)} for i in range(n)]},
        actual={"kind": "rows", "rows": [{"id": str(i)} for i in range(n)]},
    )


# ---- _slt_sidecar_path ------------------------------------------------------

def test_sidecar_path_is_junit_sibling_with_slt_json_stem():
    p = _slt_sidecar_path(_config("/tmp/.results/live-foo.xml"))
    assert str(p) == "/tmp/.results/live-foo.slt.json"


def test_sidecar_path_none_without_xmlpath():
    assert _slt_sidecar_path(_config(None)) is None
    assert _slt_sidecar_path(types.SimpleNamespace()) is None


# ---- _slt_collect_report ----------------------------------------------------

def test_collect_passing_call_records_test_with_assertions():
    config = _config("/tmp/x.xml")
    item = _item("regression/op/a/test.yaml", "a", [_data_record("passed")],
                 topology="cluster", services=["postgres"])
    _slt_collect_report(config, item, _report("call", "passed", duration=2.5))
    tr = config._slt_results[item.nodeid]
    assert tr["status"] == "passed"
    assert tr["topology"] == "cluster"
    assert tr["services"] == ["postgres"]
    assert tr["duration"] == 2.5
    assert tr["skip_reason"] is None
    assert len(tr["assertions"]) == 1
    # sidecar path recorded onto the item for junit's <properties>
    assert ("slt_result_json", "/tmp/x.slt.json") in item.user_properties


def test_collect_failing_call_marks_failed():
    config = _config("/tmp/x.xml")
    item = _item("n", "a", [_data_record("failed")])
    _slt_collect_report(config, item, _report("call", "failed"))
    assert config._slt_results["n"]["status"] == "failed"


def test_collect_skipped_call_captures_reason_and_no_assertions():
    config = _config("/tmp/x.xml")
    item = _item("n", "a", [])
    lr = ("regression/op/a/test.yaml", 10, "Skipped: set SLT_GCS=1")
    _slt_collect_report(config, item, _report("call", "skipped", duration=0.0, longrepr=lr))
    tr = config._slt_results["n"]
    assert tr["status"] == "skipped"
    assert tr["skip_reason"] == "set SLT_GCS=1"
    assert tr["assertions"] == []


def test_collect_setup_failure_marks_error():
    config = _config("/tmp/x.xml")
    item = _item("n", "a", [])
    _slt_collect_report(config, item, _report("setup", "failed"))
    assert config._slt_results["n"]["status"] == "error"


def test_collect_setup_pass_defers_to_call():
    config = _config("/tmp/x.xml")
    item = _item("n", "a", [_data_record("passed")])
    _slt_collect_report(config, item, _report("setup", "passed"))
    assert "n" not in getattr(config, "_slt_results", {})
    _slt_collect_report(config, item, _report("call", "passed"))
    assert config._slt_results["n"]["status"] == "passed"


def test_collect_teardown_failure_downgrades_passed_to_error():
    config = _config("/tmp/x.xml")
    item = _item("n", "a", [_data_record("passed")])
    _slt_collect_report(config, item, _report("call", "passed"))
    _slt_collect_report(config, item, _report("teardown", "failed"))
    assert config._slt_results["n"]["status"] == "error"


# ---- _slt_write_sidecar -----------------------------------------------------

def test_write_sidecar_assembles_schema_valid_doc(tmp_path):
    xml = tmp_path / "live-foo.xml"
    config = _config(str(xml))
    # passing, failing, skipped tests
    _slt_collect_report(config, _item("p", "pass", [_data_record("passed")]),
                        _report("call", "passed"))
    _slt_collect_report(config, _item("f", "fail", [_data_record("failed")]),
                        _report("call", "failed"))
    _slt_collect_report(config, _item("s", "skip", []),
                        _report("call", "skipped", longrepr=("x", 1, "Skipped: nope")))
    _slt_write_sidecar(config)

    doc = json.loads((tmp_path / "live-foo.slt.json").read_text())
    validate(doc)  # must not raise
    by_status = {t["status"] for t in doc["tests"]}
    assert by_status == {"passed", "failed", "skipped"}
    assert len(doc["tests"]) == 3


def test_write_sidecar_truncates_large_row_sets(tmp_path):
    xml = tmp_path / "live-foo.xml"
    config = _config(str(xml))
    rows = [{"id": str(i)} for i in range(500)]
    rec = build_assertion_result(
        type="data", status="failed",
        spec={"target": "t", "match": "m"}, target="t", db="postgres", detail="mismatch",
        expected={"kind": "rows", "rows": rows}, actual={"kind": "rows", "rows": rows},
    )
    _slt_collect_report(config, _item("n", "big", [rec]), _report("call", "failed"))
    _slt_write_sidecar(config)

    doc = json.loads((tmp_path / "live-foo.slt.json").read_text())
    validate(doc)
    a = doc["tests"][0]["assertions"][0]
    assert a["expected"]["truncated"] is True
    assert a["expected"]["count"] == 500
    assert len(a["expected"]["rows"]) < 500


def test_write_sidecar_noop_without_results_or_path(tmp_path):
    # no xmlpath -> nothing written, no raise
    _slt_write_sidecar(_config(None))
    # xmlpath but no results -> nothing written, no raise
    xml = tmp_path / "live-foo.xml"
    _slt_write_sidecar(_config(str(xml)))
    assert not (tmp_path / "live-foo.slt.json").exists()
