"""The run record ``run-evidence.json`` (C4 1.9.0) binds a tier's evidence to its fresh JUnit
invocation. Each test first produces a tier dir from a real plugin execution -- pytester through striim-test's own
guard (``striim_test.pytest_guard``, which writes selection.json/results.json) and ``livetest.plugin``, with
``--junitxml`` and ``SLT_INVOCATION_ID``; fakes only at the infrastructure edges (tests/lifecycle/exec_harness.py)
-- then runs the real checker (``livetest.evidence.check``), after at most one mutation of the artifacts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from livetest import evidence

pytest_plugins = ["pytester"]   # run_case drives a fresh pytest process through pytester

# -p xdist.plugin assumes plugin autoload is off, as in striim-test's live child; with it on, xdist is
# registered twice and the child pytest cannot start.
XDIST_ENV = {"SLT_PARALLEL": "1"}   # run_case's child already has plugin autoload off

INV = "5c1e0d2a9b8f47e6a1d3c5b7e9f01234"
OTHER = "ffffffffffffffffffffffffffffffff"


def _h():
    import _slt_exec_harness as h
    return h


def produce(run_case, case="initial-load", scenario=None, env=None, **kw):
    run = run_case(case, {"initial_load": True} if scenario is None else scenario, guard=True,
                   env={"SLT_INVOCATION_ID": INV, **(env or {})}, **kw)
    import inttest
    import livetest
    import striim_test
    manifest = run.root / "gold-targets.yaml"
    identity = run.root / "identity.json"
    identity.write_text(json.dumps({
        "schemaVersion": 1, "tool": "striim-test", "interpreter": sys.executable, "mode": "sibling",
        "packages": {m.__name__: str(Path(m.__file__).resolve().parent) for m in (livetest, inttest, striim_test)},
        "manifest": {"path": str(manifest), "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
        "runEpoch": _h().RUN, "infraOwnership": "shared",
        "framework": {"wheelSha256": {"reason": "framework mode sibling: no wheel or lock digest"},
                      "lockSha256": {"reason": "framework mode sibling: no wheel or lock digest"}}}))
    return run, run.root / "live", identity


def checked(tier, identity, invocation=INV):
    return evidence.check(tier, invocation, identity, label="live")[1]


def failing(record) -> set:
    return {c["name"] for c in record["checks"] if not c["ok"]}


def _envelope(tier) -> Path:
    [path] = (tier / "evidence").glob("*/*/evidence.json")
    return path


def _edit_json(path: Path, mutate) -> None:
    doc = json.loads(path.read_text())
    mutate(doc)
    path.write_text(json.dumps(doc))


# ---------------------------------------------------------------- fresh

def test_fresh_run_valid_and_qualified(run_case):
    run, tier, identity = produce(run_case)
    _h()._agree(run, "passed", qualifies=True)
    p = subprocess.run([sys.executable, "-m", "livetest.evidence", "check", "--tier-dir", str(tier), "--invocation", INV,
                        "--identity", str(identity), "--label", "live"], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    record = json.loads((tier / "run-evidence.json").read_text())
    assert record["valid"] is True and record["qualified"] is True, record["checks"]
    assert [c["name"] for c in record["checks"]] == list(evidence.RUN_CHECKS) and failing(record) == set()
    assert record["kind"] == "run" and record["invocation"] == INV and record["runId"] == _h().RUN
    assert record["reports"]["junit"]["invocationProperty"] == INV and record["reports"]["junit"]["tests"] == 1
    assert record["selection"]["selected"] == ["live:cases/initial-load::initial-load"] and record["selection"]["notExecuted"] == []
    [case] = record["cases"]
    assert case["qualifies"] is True and case["nodeid"] == "cases/initial-load/test.yaml::initial-load"
    assert case["envelope"]["sha256"] == "sha256:" + hashlib.sha256(_envelope(tier).read_bytes()).hexdigest()


# ---------------------------------------------------------------- JUnit problems

def test_missing_junit_invalid(run_case):
    _run, tier, identity = produce(run_case)
    (tier / "junit.xml").unlink()
    record = checked(tier, identity)
    assert record["valid"] is False and {"junit-present", "junit-wellformed", "junit-invocation"} <= failing(record)


def test_malformed_junit_invalid(run_case):
    _run, tier, identity = produce(run_case)
    data = (tier / "junit.xml").read_bytes()
    (tier / "junit.xml").write_bytes(data[: len(data) // 2])
    record = checked(tier, identity)
    assert "junit-wellformed" in failing(record) and record["valid"] is False


def test_junit_from_another_invocation_invalid(run_case):
    _run, tier, identity = produce(run_case)
    text = (tier / "junit.xml").read_text()
    assert text.count(f'name="slt_invocation" value="{INV}"') == 1
    (tier / "junit.xml").write_text(text.replace(f'name="slt_invocation" value="{INV}"', f'name="slt_invocation" value="{OTHER}"'))
    record = checked(tier, identity)
    assert failing(record) == {"junit-invocation"}, record["checks"]


def test_junit_over_cap_bounded_invalid(run_case, monkeypatch):
    _run, tier, identity = produce(run_case)
    monkeypatch.setattr(evidence, "JUNIT_CAP", 64)
    import xml.etree.ElementTree as ET
    monkeypatch.setattr(ET, "parse", lambda *a, **k: pytest.fail("an over-cap JUnit must not be parsed"))
    record = checked(tier, identity)
    detail = next(c["detail"] for c in record["checks"] if c["name"] == "junit-wellformed")
    assert "junit-wellformed" in failing(record) and "over the 64-byte cap" in detail


# ---------------------------------------------------------------- envelope problems

def test_prior_run_envelope_in_evidence_dir_invalid(run_case):
    _run, tier, identity = produce(run_case)
    src = _envelope(tier)
    prior = src.parent.parent / "20260101T000000Z-00000000" / "evidence.json"
    prior.parent.mkdir()
    shutil.copy(src, prior)

    def older(doc):
        doc["run"]["runId"] = "20260101T000000Z-00000000"
        doc["run"]["invocation"] = doc["reports"][0]["invocation"] = OTHER
    _edit_json(prior, older)
    record = checked(tier, identity)
    assert "no-foreign-envelopes" in failing(record) and record["valid"] is False


def test_envelope_case_identity_mismatch_invalid(run_case):
    _run, tier, identity = produce(run_case)
    src = _envelope(tier)
    moved = tier / "evidence" / "other" / src.parent.name / "evidence.json"
    moved.parent.mkdir(parents=True)
    shutil.move(src, moved)
    _edit_json(moved, lambda doc: doc["run"].__setitem__("nodeid", "cases/other/test.yaml::other"))
    record = checked(tier, identity)
    assert {"case-identity", "one-envelope-per-executed-case"} <= failing(record)


def test_missing_required_case_envelope_invalid(run_case):
    _run, tier, identity = produce(run_case)
    _envelope(tier).unlink()
    record = checked(tier, identity)
    assert "one-envelope-per-executed-case" in failing(record) and record["valid"] is False


def test_partial_4_1_envelope_invalid(run_case):
    _run, tier, identity = produce(run_case)
    path = _envelope(tier)

    def as_41(doc):
        for key in ("kind", "data", "integrity"):
            doc.pop(key)
        for key in ("invocation", "nodeid"):
            doc["run"].pop(key)
        doc["partial"] = {"by": "legacy writer", "missing": evidence.PARTIAL_MISSING_41}
    _edit_json(path, as_41)
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record)
    assert any("partial" in d for c in record["checks"] if c["name"] == "envelope-valid" for d in c["detail"])


@pytest.mark.parametrize("source", ["junit", "envelope", "v1"])
def test_status_disagreement_junit_vs_envelope_vs_v1_invalid(run_case, source):
    _run, tier, identity = produce(run_case)
    if source == "junit":
        text = (tier / "junit.xml").read_text()
        text = re.sub(r"(<testcase [^>]*>)", r'\1<failure message="edited">edited</failure>', text, count=1)
        (tier / "junit.xml").write_text(text)
    elif source == "envelope":
        def failed(doc):
            doc["run"].update(status="failed", qualifies=False, qualifiesReason="run status failed")
        _edit_json(_envelope(tier), failed)
    else:
        _edit_json(tier / "junit.slt.json", lambda doc: doc["tests"][0].__setitem__("status", "failed"))
    record = checked(tier, identity)
    assert "counts-agree" in failing(record), record["checks"]
    detail = next(c["detail"] for c in record["checks"] if c["name"] == "counts-agree")
    assert "'failed'" in detail[0] and "'passed'" in detail[0]                 # both sides named, neither picked


# ---------------------------------------------------------------- missing records

def test_missing_cleanup_record_invalid(run_case):
    _run, tier, identity = produce(run_case)
    _edit_json(_envelope(tier), lambda doc: doc["lifecycle"].pop("cleanup"))
    record = checked(tier, identity)
    assert {"cleanup-record", "envelope-valid"} <= failing(record)


def test_missing_or_changed_identity_invalid(run_case, tmp_path):
    _run, tier, identity = produce(run_case)
    saved = identity.read_text()
    identity.unlink()
    assert "identity" in failing(checked(tier, identity))
    (tier / "run-evidence.json").unlink()
    doc = json.loads(saved)
    doc["runEpoch"] = "20990101T000000Z-99999999"
    identity.write_text(json.dumps(doc))
    assert "identity" in failing(checked(tier, identity))


def test_missing_input_hash_invalid(run_case):
    _run, tier, identity = produce(run_case)
    _edit_json(_envelope(tier), lambda doc: doc["inputs"].pop("logicalInputsSha256"))
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record) and record["valid"] is False


# ---------------------------------------------------------------- skips

def test_unexplained_skip_valid_but_not_qualified(run_case):
    _run, tier, identity = produce(run_case, env={"SLT_SKIP_VERIFY": "1"})
    record = checked(tier, identity)
    assert record["valid"] is True, record["checks"]
    assert record["qualified"] is False and record["qualifiedReason"].startswith("skipped")
    assert record["selection"]["skipped"][0]["id"] == "live:cases/initial-load::initial-load"


def test_deselected_disabled_case_is_not_a_skip(run_case):
    def prepare(root, name):
        off = root / "cases" / "off"
        shutil.copytree(root / "cases" / "initial-load", off)
        text = (off / "test.yaml").read_text()
        (off / "test.yaml").write_text(re.sub(r"^name: .*$", "name: lc-off\ndisabled: known bug 123", text, count=1, flags=re.M))
    _run, tier, identity = produce(run_case, prepare=prepare)
    record = checked(tier, identity)
    assert record["valid"] is True and record["qualified"] is True, (record["checks"], record["qualifiedReason"])
    assert record["selection"]["skipped"] == []
    assert [d["id"] for d in record["selection"]["deselected"]] == ["live:cases/off::off"]


# ---------------------------------------------------------------- other

def test_evidence_error_property_invalid(run_case):
    blocked = []

    def prepare(root, name):
        d = root / "live" / "evidence"
        d.mkdir(parents=True)
        d.chmod(0o555)
        blocked.append(d)
    try:
        run, tier, identity = produce(run_case, prepare=prepare)
        assert run.ret == 1, run.text
        record = checked(tier, identity)
    finally:
        for d in blocked:
            d.chmod(0o755)
    assert {"no-evidence-errors", "one-envelope-per-executed-case"} <= failing(record)


def test_run_record_is_exclusive(run_case):
    _run, tier, identity = produce(run_case)
    checked(tier, identity)
    before = (tier / "run-evidence.json").read_bytes()
    with pytest.raises(evidence.EvidenceError) as ei:
        checked(tier, identity)
    assert ei.value.code == "evidence-exists" and (tier / "run-evidence.json").read_bytes() == before


def test_xdist_two_workers_one_invocation_valid(run_case):
    def prepare(root, name):
        two = root / "cases" / "legacy-two"
        shutil.copytree(root / "cases" / "legacy", two)
        text = (two / "test.yaml").read_text()
        (two / "test.yaml").write_text(re.sub(r"^name: .*$", "name: lc-legacy-two", text, count=1, flags=re.M))
    _run, tier, identity = produce(run_case, "legacy", scenario={}, prepare=prepare,
                                   args=("-n", "2", "-p", "xdist.plugin"), env=XDIST_ENV)
    assert sorted(p.name for p in tier.glob("selection-*.json")) == ["selection-gw0.json", "selection-gw1.json"]
    record = checked(tier, identity)
    assert record["valid"] is True, [c for c in record["checks"] if not c["ok"]]
    assert len(record["cases"]) == 2 and record["reports"]["junit"]["invocationProperty"] == INV
    assert {c["nodeid"] for c in record["cases"]} == {"cases/legacy/test.yaml::legacy", "cases/legacy-two/test.yaml::legacy-two"}


def test_xdist_sidecar_record_missing_invalid(run_case):
    """Once worker records reach the controller's sidecar, a missing record is a counts-agree failure."""
    def prepare(root, name):
        two = root / "cases" / "legacy-two"
        shutil.copytree(root / "cases" / "legacy", two)
        text = (two / "test.yaml").read_text()
        (two / "test.yaml").write_text(re.sub(r"^name: .*$", "name: lc-legacy-two", text, count=1, flags=re.M))
    _run, tier, identity = produce(run_case, "legacy", scenario={}, prepare=prepare,
                                   args=("-n", "2", "-p", "xdist.plugin"), env=XDIST_ENV)
    _edit_json(tier / "junit.slt.json", lambda doc: doc.__setitem__("tests", doc["tests"][:1]))
    record = checked(tier, identity)
    assert "counts-agree" in failing(record)
    detail = next(c["detail"] for c in record["checks"] if c["name"] == "counts-agree")
    assert len(detail) == 1 and "'v1': None" in detail[0]


# ---------------------------------------------------------------- review round 1

def test_disagreeing_comparison_hashes_invalid(run_case):
    """F2: a comparison that claims equal while its expected and actual digests differ is not a valid envelope."""
    from livetest import exactdata
    _run, tier, identity = produce(run_case)

    def mutate(doc):
        doc["data"]["comparisons"][0]["expected"]["sha256"] = "sha256:" + "f" * 64
        doc["data"].update(exactdata.aggregates(doc["data"]["comparisons"]))
    _edit_json(_envelope(tier), mutate)
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record) and record["valid"] is False and record["qualified"] is False


def test_null_observed_identities_invalid(run_case):
    """F2: null observed identities (a docker runtime's version and image id, the source) are not valid values."""
    _run, tier, identity = produce(run_case)

    def mutate(doc):
        doc["runtime"]["striim"]["observed"].update(version=None, imageId=None)
        doc["inputs"]["source"] = None
    _edit_json(_envelope(tier), mutate)
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record) and record["valid"] is False and record["qualified"] is False


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_missing_or_malformed_sidecar_invalid(run_case, damage):
    """F3: the v1 sidecar is one of the reconciled records; without a schema-valid one the counts cannot agree."""
    _run, tier, identity = produce(run_case)
    sidecar = tier / "junit.slt.json"
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.write_bytes(sidecar.read_bytes()[:20])
    record = checked(tier, identity)
    assert "counts-agree" in failing(record) and record["valid"] is False, record["checks"]


def test_unmatched_junit_testcase_invalid(run_case):
    """F3: a JUnit testcase that no selected case, sidecar record or envelope accounts for fails reconciliation."""
    import xml.etree.ElementTree as ET
    _run, tier, identity = produce(run_case)
    path = tier / "junit.xml"
    tree = ET.parse(path)
    suite = next(tree.getroot().iter("testsuite"))
    case = ET.SubElement(suite, "testcase", classname="cases.other.test.yaml", name="unmatched-broken-case")
    ET.SubElement(case, "failure", message="unmatched failure")
    suite.set("tests", str(int(suite.get("tests")) + 1))
    suite.set("failures", str(int(suite.get("failures", "0")) + 1))
    tree.write(path)
    record = checked(tier, identity)
    assert "counts-agree" in failing(record) and record["valid"] is False and record["qualified"] is False


# ---------------------------------------------------------------- review round 2

def _invalid_declaration(c):
    from livetest import canon
    c["declaration"]["columns"] = {"id": "not-a-type"}
    c["declarationSha256"] = canon.declaration_sha256(c["declaration"])       # the hash agrees; the semantics do not


_NESTED = {
    "source-string": lambda doc, c: doc["inputs"].update(source="unknown"),
    "product-no-digest": lambda doc, c: doc["inputs"].update(
        productBuild={"artifacts": [{"name": "missing.jar", "sha256": None}]}),
    "expected-and-observed-version": lambda doc, c: (
        doc["runtime"]["striim"].update(expected={"unexpected": "release"}),
        doc["runtime"]["striim"]["observed"].update(version="wrong-version")),
    "expected-without-release": lambda doc, c: doc["runtime"]["striim"].update(expected={"unexpected": "release"}),
    "observed-bogus-version": lambda doc, c: doc["runtime"]["striim"]["observed"].update(version="wrong-version"),
    "missing-golden-digest": lambda doc, c: c["expected"].update(source=None, templateSha256=None),
    "invalid-declaration": lambda doc, c: _invalid_declaration(c),
}


@pytest.mark.parametrize("mutation", list(_NESTED))
def test_incomplete_nested_witness_invalid(run_case, mutation):
    """R2-1 (F2): after a real qualifying run, a structured witness replaced by an arbitrary string or dictionary, an
    artifact without its digest, a release without its version, a data comparison without its golden witness, or a
    declaration C8.2 refuses (with its hash recomputed) is not a valid envelope, through read and the run checker."""
    _run, tier, identity = produce(run_case)
    _edit_json(_envelope(tier), lambda doc: _NESTED[mutation](doc, doc["data"]["comparisons"][0]))
    with pytest.raises(evidence.EvidenceError):
        evidence.read(_envelope(tier))
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record) and record["valid"] is False and record["qualified"] is False, record["checks"]


# ---------------------------------------------------------------- review round 3

def _rendered_digests_null(doc):
    assert doc["inputs"]["renderedInputs"], "the run recorded no sent inputs"
    for entry in doc["inputs"]["renderedInputs"]:
        entry["templateSha256"] = entry["renderedSha256"] = None


def _tql_digest_null(doc):
    assert doc["inputs"]["caseAssets"]["tqlSha256"], "the case deployed no TQL"
    doc["inputs"]["caseAssets"]["tqlSha256"] = None


def _golden_integrity_null(doc):
    assert doc["integrity"]["goldens"], "the case snapshotted no golden"
    for g in doc["integrity"]["goldens"].values():
        g["inputSha256"] = g["finalSha256"] = None              # the unchanged claim keeps no digests


def _diff_disguise(doc):
    c = doc["data"]["comparisons"][0]
    c["type"] = "diff"                                          # a data comparison with no source table witness
    c["expected"].update(source="unknown", templateSha256=None)


_R3_NESTED = {"rendered-digests-null": _rendered_digests_null, "tql-digest-null": _tql_digest_null,
              "golden-integrity-null": _golden_integrity_null, "diff-disguise": _diff_disguise}


@pytest.mark.parametrize("mutation", list(_R3_NESTED))
def test_missing_nested_witness_invalid(run_case, mutation):
    """R3-1 (F2, continuing R2-1): after a real qualifying run, an envelope whose nested witnesses were removed -- the
    rendered inputs' digests, the TQL digest of a case that deployed one, the golden integrity digests behind an
    unchanged claim -- or whose successful data comparison is disguised as an unwitnessed diff is not valid, through
    read and the run checker."""
    _run, tier, identity = produce(run_case)
    _edit_json(_envelope(tier), _R3_NESTED[mutation])
    with pytest.raises(evidence.EvidenceError):
        evidence.read(_envelope(tier))
    record = checked(tier, identity)
    assert "envelope-valid" in failing(record) and record["valid"] is False and record["qualified"] is False, record["checks"]


def _counts_detail(record) -> list:
    return next(c["detail"] for c in record["checks"] if c["name"] == "counts-agree") or []


@pytest.mark.parametrize("damage", ["duplicate-pass", "duplicate-teardown-error", "outcome-after-setup-error",
                                    "error-in-call-phase"])
def test_results_multiplicity_invalid(run_case, damage):
    """R2-5 (F3): results.json's raw records are validated per phase before their statuses are folded: a repeated
    outcome, a repeated phase error, an outcome after a setup error, or an error outside setup/teardown fails
    reconciliation by name, even where folding would leave the status unchanged."""
    _run, tier, identity = produce(run_case)

    def mutate(doc):
        [rec] = doc["passed"]
        if damage == "duplicate-pass":
            doc["passed"].append(dict(rec))
        elif damage == "duplicate-teardown-error":
            doc["errors"] += [{"nodeid": rec["nodeid"], "when": "teardown"}, {"nodeid": rec["nodeid"], "when": "teardown"}]
        elif damage == "outcome-after-setup-error":
            doc["errors"].append({"nodeid": rec["nodeid"], "when": "setup"})
        else:
            doc["errors"].append({"nodeid": rec["nodeid"], "when": "call"})
    _edit_json(tier / "results.json", mutate)
    record = checked(tier, identity)
    assert "counts-agree" in failing(record) and record["valid"] is False and record["qualified"] is False
    assert any("results.json" in p and "missing or unreadable" not in p for p in _counts_detail(record)), record["checks"]


def test_results_multiplicity_allows_phase_combinations():
    """R2-5 (F3): the legitimate per-phase combinations stay valid: an outcome with a teardown error, a setup skip,
    setup and teardown errors, and one outcome per nodeid across different nodeids."""
    n = "cases/a/test.yaml::a"
    for results in ({"passed": [{"nodeid": n}], "errors": [{"nodeid": n, "when": "teardown"}]},
                    {"skipped": [{"nodeid": n, "reason": "r"}], "errors": [{"nodeid": n, "when": "teardown"}]},
                    {"errors": [{"nodeid": n, "when": "setup"}, {"nodeid": n, "when": "teardown"}]},
                    {"failed": [{"nodeid": n}], "xfailed": [{"nodeid": "cases/b/test.yaml::b"}]}):
        assert evidence._results_multiplicity(results) == [], results
    assert evidence._results_multiplicity({"passed": [{"nodeid": n}], "xfailed": [{"nodeid": n}]})


def test_exec_runtime_row_value_spelling_a_column_type_is_redacted(run_case):
    """The schema's grammar exemption covers the two declaration paths the schema itself
    fixes, never a runtime value. Through the real guard, plugin, writer and checker: a legacy data assertion keeps
    its raw result row, whose password happens to spell a canonical column type (``binary:base64``). The run still
    passes at rc 0 and qualifies, and the retained row value is redacted in the evidence the checker accepts."""
    secret = "binary:base64"

    def prepare(root, name):
        manifest = root / "cases/initial-load/test.yaml"
        manifest.write_text(manifest.read_text().replace("lifecycle:\n", (
            '    - target: "${PG_TARGET_SCHEMA}.${TID}tgt"\n'
            "      target_db: postgres-target\n"
            "      match: expected/row.csv\n"
            "lifecycle:\n")))
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["columns"])
        writer.writerow([str({"password": secret})])
        (manifest.parent / "expected/row.csv").write_text(stream.getvalue())
        conf = root / "conftest.py"
        conf.write_text(conf.read_text() + (
            "\nP.PgAdmin.count_rows = lambda self, target: 1\n"
            f"P.PgAdmin.select_rows = lambda self, target: [{{'columns': {{'password': {secret!r}}}}}]\n"))

    run, tier, identity = produce(run_case, env={"REVIEW_PASSWORD": secret}, prepare=prepare)
    assert run.ret == 0, run.text
    doc = json.loads(_envelope(tier).read_text())
    rec = checked(tier, identity)
    assert evidence.read(_envelope(tier)).qualifies
    value = next(r["actual"]["rows"][0]["columns"]["password"] for r in doc["assertions"]
                 if (r.get("actual") or {}).get("kind") == "rows")
    assert value == evidence.REDACTED, (value, rec["valid"], rec["qualified"])
