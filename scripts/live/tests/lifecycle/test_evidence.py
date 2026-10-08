"""The evidence envelope v2 subset the legacy writer owns (C4, C7.3-C7.4).

Written once from the report hook for every outcome, beside the junit, with the full run id, a partial
marker for complete-envelope fields, case asset hashes, the lifecycle records and one final outcome."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import evidence, inputs, lifecycle, runident
from livetest.manifest import load_manifest
from livetest.plugin import _slt_collect_report, _slt_write_sidecar
from livetest.resultschema import build_assertion_result, validate

RUN = "20260914T230000Z-abcd1234"


@pytest.fixture(autouse=True)
def _consumer_outside_git(monkeypatch, tmp_path):
    # inputs.source reads the consumer project (paths.project_root()), which defaults to this
    # checkout; point it at tmp_path so the envelope sees a project outside any git worktree.
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))


def _case(tmp_path):
    case = tmp_path / "cases" / "lc"
    (case / "expected").mkdir(parents=True)
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\n")
    (case / "expected" / "tgt.csv").write_text("id\n1\n")
    (case / "test.yaml").write_text("name: lc\npurpose: p\ntql: app.tql\nrequires: [postgres]\nassert: {smoke: true}\n")
    return case


def _item(tmp_path, xmlpath=True, state=None):
    case = _case(tmp_path)
    config = SimpleNamespace(option=SimpleNamespace(xmlpath=str(tmp_path / "live" / "junit.xml") if xmlpath else None),
                             rootpath=tmp_path, _slt_infra=None, _slt_striim=None)
    return SimpleNamespace(config=config, name="lc", nodeid="cases/lc/test.yaml::lc", path=case / "test.yaml",
                           manifest_path=case / "test.yaml", _slt_records=[], _slt_topology="single",
                           _slt_services=["postgres"], user_properties=[],
                           _slt_lc=state or lifecycle.State("cdc"),
                           _slt_ident=runident.derive("lc", {"SLT_RUN_EPOCH": RUN}, attempt="0a1b2c3d"))


def _report(when="call", outcome="passed", text=""):
    return SimpleNamespace(when=when, outcome=outcome, longrepr=text or None, longreprtext=text,
                           user_properties=[], duration=0.1)


def _satisfied(kind):
    return {"kind": kind, "condition": "c", "witness": "w", "at": "t", "startedAt": "t", "endedAt": "t",
            "deadlineS": 1.0, "observations": [], "reason": "satisfied"}


def _written(item):
    return json.loads(Path(item._slt_evidence_path).read_text())


def test_envelope_path_and_full_run_id(tmp_path):
    p = evidence.envelope_path("/r/live/junit.xml", "lc case/x", RUN)
    assert p == Path("/r/live/evidence/lc_case_x/20260914T230000Z-abcd1234/evidence.json")
    item = _item(tmp_path)
    evidence.finalize_from_report(item, _report())
    doc = _written(item)
    assert doc["run"]["runId"] == RUN and doc["run"]["attempt"] == "0a1b2c3d"
    assert doc["run"]["caseId"] == "live:cases/lc::lc" and doc["lifecycle"]["identity"]["runId"] == RUN
    assert item._slt_evidence_path == tmp_path / "live" / "evidence" / "lc" / RUN / "evidence.json"


def test_complete_envelope_has_no_partial_and_no_pending_reason(tmp_path):
    # The complete envelope replaces the legacy partial marker: complete keys, reasons only where allowlisted.
    item = _item(tmp_path)
    evidence.finalize_from_report(item, _report())
    doc = _written(item)
    assert "partial" not in doc and set(doc) == evidence.TOP_KEYS and doc["kind"] == "case"
    assert "computed later" not in json.dumps(doc) and evidence._reason_violations(doc) == []
    assert doc["data"] == {"reason": evidence.NO_DATA_REASON} and doc["inputs"]["source"] == {"reason": "not-a-git-worktree"}
    evidence.validate_case(doc)


def test_case_asset_hashes_computed(tmp_path):
    # every referenced file from the input snapshot, goldens outside expected/ included; an
    # unreferenced file under expected/ is not an input.
    item = _item(tmp_path)
    case = tmp_path / "cases" / "lc"
    (case / "other").mkdir()
    (case / "other" / "g.csv").write_text("id\n1\n")
    (case / "ddl.sql").write_text("CREATE TABLE ${TID}tgt (id int);\n")
    (case / "test.yaml").write_text(
        "name: lc\npurpose: p\ntql: app.tql\nrequires: [postgres]\nddl:\n  - file: ddl.sql\n    db: postgres-target\n"
        "assert:\n  data:\n    - {target: qatarget.t, target_db: postgres-target, match: other/g.csv}\n")
    item._slt_inputs = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    evidence.finalize_from_report(item, _report())
    assets = _written(item)["inputs"]["caseAssets"]
    sha = lambda p: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()   # noqa: E731
    assert assets == {"manifestSha256": sha(case / "test.yaml"), "tqlSha256": sha(case / "app.tql"),
                      "goldens": {"other/g.csv": sha(case / "other" / "g.csv")}, "files": {"ddl.sql": sha(case / "ddl.sql")}}


def _good_state():
    s = lifecycle.State("cdc")
    s.ready, s.completion = _satisfied("source-progress"), _satisfied("row-count")
    return s


def test_final_outcome_single_source_of_truth():
    verified = {"cleanupVerified": True, "verificationGaps": []}
    status, q, why = evidence.final_outcome("passed", _good_state(), {"status": "failed", "detail": "drop"}, verified)
    assert (status, q) == ("failed", False) and why == "run status failed"
    status, q, why = evidence.final_outcome("failed", _good_state(), {"status": "failed", "detail": "drop"}, verified)
    assert (status, q, why) == ("failed", False, "run status failed")
    assert evidence.final_outcome("passed", _good_state(), {"status": "ok"}, verified) == ("passed", True, None)


def test_qualifies_false_for_legacy_skipped_and_unverified():
    ok, verified = {"status": "ok"}, {"cleanupVerified": True, "verificationGaps": []}
    legacy = lifecycle.State("legacy")
    assert evidence.final_outcome("passed", legacy, ok, verified)[1:] == (False, "legacy lifecycle (no lifecycle block) never qualifies")
    assert evidence.final_outcome("skipped", _good_state(), ok, verified)[1] is False
    assert evidence.final_outcome("passed", _good_state(), {"status": "skipped", "detail": "kept"}, verified)[1] is False
    assert evidence.final_outcome("passed", _good_state(), ok, {"cleanupVerified": False, "verificationGaps": []})[2] == "cleanup not verified"
    assert evidence.final_outcome("passed", _good_state(), evidence.NO_CLEANUP_RECORD, verified)[1] is False


def test_junit_user_properties_point_to_envelope_and_qualifies(tmp_path):
    item, rep = _item(tmp_path), _report()
    path = evidence.finalize_from_report(item, rep)
    want = [("slt_evidence_json", str(path)), ("slt_qualifies", "false")]
    assert item.user_properties == want and rep.user_properties == want
    assert path.is_file() and _written(item)["run"]["qualifiesReason"].startswith("readiness witness not satisfied")


def test_v1_sidecar_validate_still_passes(tmp_path):
    item = _item(tmp_path)
    item._slt_records = [build_assertion_result(type="smoke", status="passed", spec={}, detail="RUNNING")]
    rep = _report()
    _slt_collect_report(item.config, item, rep)
    evidence.finalize_from_report(item, rep)
    _slt_write_sidecar(item.config)
    doc = json.loads((tmp_path / "live" / "junit.slt.json").read_text())
    validate(doc)
    assert doc["tests"][0]["status"] == "passed" and set(doc["tests"][0]) == {
        "name", "nodeid", "status", "topology", "services", "duration", "skip_reason", "assertions"}
    assert _written(item)["assertions"] == doc["tests"][0]["assertions"]


def test_no_junit_no_envelope_no_crash(tmp_path):
    item = _item(tmp_path, xmlpath=False)
    assert evidence.finalize_from_report(item, _report()) is None
    assert not list(tmp_path.rglob("evidence.json")) and item.user_properties == []
    broken = _item(tmp_path / "b")
    broken.manifest_path = tmp_path / "missing" / "test.yaml"
    broken._slt_lc = None
    broken._slt_ident = None
    assert evidence.finalize_from_report(broken, _report()).is_file()     # a record even without a manifest


@pytest.mark.parametrize("when,outcome,status", [("call", "passed", "passed"), ("call", "failed", "failed"),
                                                 ("call", "skipped", "skipped"), ("setup", "failed", "error")])
def test_finalize_from_report_pass_fail_error_skip(tmp_path, when, outcome, status):
    item = _item(tmp_path)
    assert evidence.finalize_from_report(item, _report("setup", "passed")) is None
    text = "Skipped: disabled" if outcome == "skipped" else ("E   boom" if outcome == "failed" else "")
    path = evidence.finalize_from_report(item, _report(when, outcome, text))
    doc = _written(item)
    assert path.is_file() and doc["run"]["status"] == status and doc["run"]["qualifies"] is False
    assert (doc["run"]["skipReason"] == "disabled") == (status == "skipped")
    assert evidence.finalize_from_report(item, _report("teardown", "passed")) is None
    assert evidence.finalize_from_report(item, _report("call", "passed")) is None       # written once


def test_failed_readiness_writes_envelope_with_reason(tmp_path):
    state = lifecycle.State("cdc")
    state.ready = {**_satisfied("source-progress"), "witness": None, "reason": "deadline"}
    item = _item(tmp_path, state=state)
    evidence.finalize_from_report(item, _report("call", "failed",
                                                "E   livetest.lifecycle.LifecycleError: lifecycle readiness source-progress failed: deadline"))
    doc = _written(item)
    assert doc["run"]["status"] == "failed" and "readiness source-progress failed: deadline" in doc["run"]["failure"]
    assert doc["lifecycle"]["ready"]["reason"] == "deadline" and doc["lifecycle"]["mode"] == "cdc"


def test_failed_ddl_writes_envelope_with_cleanup_record(tmp_path):
    item = _item(tmp_path)
    evidence.finalize_from_report(item, _report("call", "failed", "E   RuntimeError: relation already exists"))
    doc = _written(item)
    assert doc["run"]["status"] == "failed" and doc["lifecycle"]["ready"] is None
    assert doc["lifecycle"]["cleanup"] == evidence.NO_CLEANUP_RECORD
    assert doc["resources"]["verificationGaps"] == [evidence.NO_LEDGER_GAP]
    ledgered = _item(tmp_path / "l")
    ledgered._slt_cleanup = {"status": "ok", "detail": None}
    ledgered._slt_resources = {"owned": [{"kind": "pg-table", "name": "qasource.t_src", "db": "postgres-source",
                                          "state": "verified-absent"}],
                               "reused": [], "foreign": [], "cleanupVerified": True, "verificationGaps": [],
                               "ledger": "/state/lifecycle/ledgers/t.json", "faultInjected": None}
    evidence.finalize_from_report(ledgered, _report("call", "failed", "E   RuntimeError: relation already exists"))
    doc2 = _written(ledgered)
    assert doc2["lifecycle"]["cleanup"] == {"status": "ok", "detail": None}
    assert doc2["resources"]["owned"][0]["state"] == "verified-absent" and doc2["resources"]["cleanupVerified"] is True
    assert doc2["resources"]["ledger"] == "/state/lifecycle/ledgers/t.json" and doc2["lifecycle"]["faultInjected"] is None


def test_original_exception_preserved_when_cleanup_also_fails(tmp_path):
    item = _item(tmp_path, state=_good_state())
    item._slt_cleanup = {"status": "failed", "detail": "DROP TABLE failed: permission denied"}
    evidence.finalize_from_report(item, _report("call", "failed", "E   ValueError: ddl boom"))
    doc = _written(item)
    assert doc["run"]["failure"] == "E   ValueError: ddl boom"
    assert doc["run"]["qualifiesReason"] == "run status failed"
    assert doc["lifecycle"]["cleanup"]["status"] == "failed"


def test_unrecorded_cleanup_status_is_rejected(tmp_path):
    # The increment-2-only transitional value is gone: the validator refuses it and nothing writes it.
    item = _item(tmp_path)
    doc = evidence.case_envelope(item, _report(), "passed")
    evidence.validate_case(doc)
    assert doc["lifecycle"]["cleanup"]["status"] in {"ok", "failed", "skipped"}
    doc["lifecycle"]["cleanup"] = {"status": "unrecorded", "detail": "legacy teardown ran"}
    with pytest.raises(ValueError, match="lifecycle.cleanup.status 'unrecorded' invalid"):
        evidence.validate_case(doc)
    with pytest.raises(ValueError, match="'unrecorded' invalid"):
        evidence.write_case_envelope(item.config.option.xmlpath, item.name, RUN, doc)
    assert not list(tmp_path.rglob("evidence.json"))
    assert evidence.CLEANUP_STATUSES == {"ok", "failed", "skipped"}
    repo = Path(__file__).resolve().parents[4]
    for rel in ("scripts/live/livetest/evidence.py", "scripts/live/livetest/ownership.py",
                "scripts/live/livetest/exactdata.py", "scripts/live/livetest/inputs.py", "scripts/live/livetest/canon.py",
                "docs/internals/design/lifecycle.md"):
        assert "unrecorded" not in (repo / rel).read_text(), rel
