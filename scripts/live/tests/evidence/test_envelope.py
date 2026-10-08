"""The complete case envelope (C4 1.9.0) -- completeness from observed inputs, reasons only
where allowlisted, the validator (which recomputes ``run.qualifies``), the reader, exclusive writes, the v1
adapter, the data section, qualification, bounded source identity, and no write path to goldens."""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import shutil
import subprocess
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import canon, evidence, exactdata, infra, inputs, lifecycle, runident
from livetest.evidence import EvidenceError
from livetest.manifest import load_manifest
from livetest.plugin import _slt_collect_report, _slt_write_sidecar
from livetest.resultschema import build_assertion_result

REPO = Path(__file__).resolve().parents[4]
RUN = "20260915T101500Z-abcd1234"


def _satisfied(kind):
    return {"kind": kind, "condition": "c", "witness": "w", "at": "t", "startedAt": "t", "endedAt": "t",
            "deadlineS": 1.0, "observations": [], "reason": "satisfied"}


def _case(root: Path, yaml_text: str | None = None) -> Path:
    case = root / "cases" / "lc"
    (case / "expected").mkdir(parents=True, exist_ok=True)
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (case / "ddl.sql").write_text("CREATE TABLE ${TID}tgt (id int);\n")
    (case / "expected" / "tgt.csv").write_text("id\n1\n2\n")
    (case / "test.yaml").write_text(yaml_text or (
        "name: lc\npurpose: p\ntql: app.tql\nrequires: [postgres]\n"
        "ddl:\n  - file: ddl.sql\n    db: postgres-target\n"
        "exact: {version: 1}\n"
        "assert:\n  data:\n    - {target: \"${PG_TARGET_SCHEMA}.${TID}tgt\", target_db: postgres-target, "
        "match: expected/tgt.csv, exact: {columns: {id: integer}}}\n"))
    return case


class _Infra:
    ownership, services = "shared", []

    def record(self):
        return {"ownership": "shared", "services": [], "striim": {}, "teardown": None}


@pytest.fixture
def good(tmp_path, monkeypatch):
    """A case item whose envelope qualifies: lifecycle witnesses satisfied, cleanup ok and verified, one equal
    owned slt-canon/1 comparison from canon itself, the golden snapshotted and unchanged, identities observed."""
    for name in ("SLT_FRAMEWORK_MODE", "GOLD_TARGETS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(infra, "_docker_inspect", lambda argv: SimpleNamespace(
        returncode=0, stdout=f"sha256:{'a' * 64}|{argv[-1]}:5.4.2\n", stderr=""))
    monkeypatch.setattr(infra, "runtime_version", lambda config, ctx, container: "5.4.2")   # a fake runtime edge (F9)
    case = _case(tmp_path)
    m = load_manifest(case / "test.yaml")
    snap = inputs.snapshot(m, case / "test.yaml")
    golden = canon.parse_golden(snap.golden(case / "expected" / "tgt.csv"), db_route=True)
    decl = canon.declaration({"columns": {"id": "integer"}}, db_route=True)
    comp = canon.compare(golden, [{"id": 1}, {"id": 2}], decl)
    entry = {**comp, "index": 0, "type": "data", "target": "qatarget.t1_tgt", "route": "postgres-target",
             "expected": {**comp["expected"], "source": "expected/tgt.csv", "templateSha256": snap.sha256(case / "expected" / "tgt.csv")},
             "actual": {**comp["actual"], "owned": True}}
    state = lifecycle.State("cdc")
    state.ready, state.completion = _satisfied("source-progress"), _satisfied("row-count")
    config = SimpleNamespace(option=SimpleNamespace(xmlpath=str(tmp_path / "live" / "junit.xml")), rootpath=tmp_path,
                             _slt_infra=_Infra(), _slt_release={"STRIIM_VERSION": "5.4.2"},
                             _slt_striim=SimpleNamespace(url="http://localhost:9080", mode="docker"))
    ident = runident.derive("lc", {"SLT_RUN_EPOCH": RUN}, attempt="0a1b2c3d")
    item = SimpleNamespace(config=config, name="lc", nodeid="cases/lc/test.yaml::lc", path=case / "test.yaml",
                           manifest_path=case / "test.yaml", _slt_topology="single", _slt_services=["postgres"],
                           user_properties=[], _slt_lc=state, _slt_ident=ident, _slt_inputs=snap, _slt_data=[entry],
                           _slt_records=[record()],
                           _slt_cleanup={"status": "ok", "detail": None},
                           _slt_resources={"owned": [], "reused": [], "foreign": [], "cleanupVerified": True,
                                           "verificationGaps": []})
    return item


def record(type="data", target="qatarget.t1_tgt", db="postgres-target", status="passed", spec=None):
    """The assertion record an exact assertion leaves for its comparison (the envelope's comparison must
    agree with it -- same type, target, route and outcome)."""
    return build_assertion_result(type=type, status=status, spec=spec or {}, target=target, db=db, detail="ok")


# A legitimate diff's record declares its source, and the attempt's ledger created it.
# The recorder writes the source it rendered, so the record declares the table itself.
DIFF_SPEC = {"source": "qasource.t1_src", "source_db": "postgres-source",
             "target": "${PG_TARGET_SCHEMA}.t1_tgt", "target_db": "postgres-target"}
SRC_OWNED = {"kind": "pg-table", "name": "qasource.t1_src", "db": "postgres-source", "state": "verified-absent"}


def report(when="call", outcome="passed", text=""):
    return SimpleNamespace(when=when, outcome=outcome, longrepr=text or None, longreprtext=text, user_properties=[], duration=0.1)


def doc_of(item, status="passed"):
    return evidence.case_envelope(item, report(), status)


def write(item, doc=None):
    doc = doc if doc is not None else doc_of(item)
    return evidence.write_case_envelope(item.config.option.xmlpath, item.name, doc["run"]["runId"], doc)


def rejected(doc, item, code=None):
    with pytest.raises(EvidenceError) as ei:
        write(item, doc)
    assert code is None or ei.value.code == code, str(ei.value)
    assert not list(Path(item.config.option.xmlpath).parent.rglob("evidence.json"))
    return ei.value


# ---------------------------------------------------------------- completeness

def test_complete_envelope_has_no_partial_and_no_pending_reason(good):
    doc = doc_of(good)
    assert set(doc) == evidence.TOP_KEYS and "partial" not in doc and doc["kind"] == "case"
    assert evidence._reason_violations(doc) == [] and evidence.forms(doc) == []
    assert "computed later" not in json.dumps(doc) and "pending" not in json.dumps(doc).lower()
    assert doc["run"]["qualifies"] is True, doc["run"]["qualifiesReason"]
    evidence.validate_case(doc)
    assert evidence.read(write(good)).qualifies is True


def test_rendered_inputs_recorded_at_send_sites_only(good):
    case = Path(good.manifest_path).parent
    snap = good._slt_inputs
    assert snap.rendered_inputs() == []                                  # nothing sent yet, nothing listed
    snap.record("ddl", "ddl.sql", "CREATE TABLE t1_tgt (id int);\n", path=case / "ddl.sql")
    snap.record("ddl", "ddl.sql", "CREATE TABLE t1_tgt (id int);\n", path=case / "ddl.sql")
    [entry] = snap.rendered_inputs()
    assert entry["uses"] == 2 and entry["templateSha256"] == snap.sha256(case / "ddl.sql")
    assert entry["renderedSha256"] == "sha256:" + hashlib.sha256(b"CREATE TABLE t1_tgt (id int);\n").hexdigest()
    assert not any(e["role"] == "tql" for e in snap.rendered_inputs())    # the TQL was snapshotted, never sent
    plugin = (REPO / "scripts/live/livetest/plugin.py").read_text()
    for send, rec in (("_run_admin_sql(entry, _slt_sql)", 'self._slt_inputs.record("ddl" if'),
                      ("_slt_out = render(text, tokens)", '_slt_inputs.record_on(m, "tql"'),
                      ("dest.write_bytes(data)", '_slt_inputs.record_on(m, "upload"')):
        assert plugin.index(send) < plugin.index(rec, plugin.index(send)) < plugin.index(send) + 400, send
    # Every server-file path transfers the staged file whose bytes are recorded (never a path re-read)
    assert '_staged = _slt_evidence.stage_on(m, "server-file", _sf, m.source_dir / _sf)' in plugin
    assert "place_server_file(ctx, _staged, _rdest)" in plugin and "place_server_file(ctx, _staged, render(_dest, tokens))" in plugin
    assert plugin.count('_slt_evidence.stage_on(m, "server-file"') == 3 and 'record_on(m, "server-file"' not in plugin
    src = (REPO / "scripts/live/livetest/lifecycle.py").read_text()
    assert src.index("writer.run(sent, watch.deadline.remaining())") < src.index('self.record("sentinel", s[file_key], sent')


def test_staged_server_file_keeps_its_mode_and_is_removed_after_placement(tmp_path):
    """The staged copy keeps the source's mode bits, as a direct docker cp or shutil.copy did, and
    unstage removes it with its temp dir; a path stage_on did not create is never touched."""
    src = tmp_path / "run.sh"
    src.write_text("#!/bin/sh\necho hi\n")
    src.chmod(0o750)
    snap = inputs.Snapshot({})
    staged = evidence.stage_on(SimpleNamespace(_slt_inputs=snap), "server-file", "run.sh", src)
    assert staged != src and staged.read_bytes() == src.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o750
    assert [r["role"] for r in snap.rendered_inputs()] == ["server-file"]
    evidence.unstage(staged)
    assert not staged.parent.exists() and src.is_file()
    evidence.unstage(src)                                                   # not a staged file: left alone
    assert src.is_file()
    missing = tmp_path / "gone.csv"
    assert evidence.stage_on(SimpleNamespace(_slt_inputs=snap), "server-file", "gone.csv", missing) == missing


def test_logical_inputs_equal_across_identities_rendered_manifest_differs(good):
    case = Path(good.manifest_path).parent
    m = load_manifest(case / "test.yaml")
    digests = []
    for tid in ("t111111111_", "t222222222_"):
        snap = inputs.snapshot(m, case / "test.yaml")
        snap.record("ddl", "ddl.sql", (case / "ddl.sql").read_text().replace("${TID}", tid), path=case / "ddl.sql")
        digests.append((snap.logical_inputs_sha256(), snap.rendered_manifest_sha256()))
    assert digests[0][0] == digests[1][0] and digests[0][1] != digests[1][1]


# ---------------------------------------------------------------- reasons and required fields

@pytest.mark.parametrize("field", ["data-with-exact", "caseAssets", "renderedManifestSha256"])
def test_reason_only_in_allowlisted_fields(good, field):
    doc = doc_of(good)
    if field == "data-with-exact":
        doc["data"] = {"reason": "computed later"}
    elif field == "caseAssets":
        doc["inputs"]["caseAssets"]["goldens"] = {"expected/tgt.csv": {"reason": "pending"}}
    else:
        doc["inputs"]["renderedManifestSha256"] = {"reason": "computed later"}
    rejected(doc, good)
    ok = doc_of(good)
    ok["inputs"]["source"] = {"reason": "not-a-git-worktree"}
    evidence.validate_case(ok)
    # only the allowlist can refuse these: the structure is valid and qualification does not change
    for section, key, value in (("inputs", "productBuild", {"reason": "pending"}),
                                ("runtime", "resourceRoot", {"reason": "not collected"})):
        bad = doc_of(good)
        bad[section][key] = value
        rejected(bad, good, "evidence-reason")


@pytest.mark.parametrize("field", ["source", "striim", "productBuild"])
def test_unreadable_or_unobserved_never_qualifies(good, monkeypatch, field):
    if field == "source":
        monkeypatch.setattr(evidence, "source_identity", lambda root, run=None: {"unreadable": "git did not answer within 5s"})
    elif field == "striim":
        good.config._slt_striim = None
    else:
        real = load_manifest(good.manifest_path)
        real.modules = [{"jar": "modules/op", "token": "OP", "kind": "op", "upload": [], "on_agent": False}]
        monkeypatch.setattr("livetest.manifest.load_manifest", lambda path: real)
    doc = doc_of(good)
    assert doc["run"]["qualifies"] is False and doc["run"]["qualifiesReason"].startswith("not observed or unreadable:")
    evidence.validate_case(doc)
    doc["run"]["qualifies"], doc["run"]["qualifiesReason"] = True, None
    rejected(doc, good, "evidence-qualifies")


def test_structured_witness_variants_valid_and_stripped_witnesses_rejected(good):
    """The legitimate variants stay valid -- a diff names its source table and has no template,
    op artifacts carry their observed digests -- while a data comparison without its template digest, a diff claiming
    one, an unwitnessed release and an artifact without its digest are rejected on write."""
    comp = good._slt_data[0]
    diff = {**comp, "type": "diff", "expected": {**comp["expected"], "source": "postgres-source:qasource.t1_src",
                                                 "templateSha256": None}}
    good._slt_data, good._slt_records = [diff], [record(type="diff", spec=DIFF_SPEC)]
    good._slt_resources["owned"] = [SRC_OWNED]
    doc = doc_of(good)
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is True, doc["run"]["qualifiesReason"]
    doc["inputs"]["productBuild"] = {"artifacts": [{"name": "op.jar", "sha256": "sha256:" + "0" * 64}]}
    evidence.validate_case(doc)
    for mutate in (lambda d: d["inputs"].update(productBuild={"artifacts": [{"name": "op.jar", "sha256": "0" * 64}]}),
                   lambda d: d["data"]["comparisons"][0]["expected"].update(templateSha256="sha256:" + "1" * 64),
                   lambda d: d["runtime"]["striim"].update(expected={"STRIIM_SERIES": "5.4"})):
        bad = doc_of(good)
        mutate(bad)
        rejected(bad, good, "evidence-invalid")
    good._slt_data, good._slt_records = [{**comp, "expected": {**comp["expected"], "templateSha256": None}}], [record()]
    rejected(doc_of(good), good, "evidence-invalid")


def test_inapplicable_nested_digests_are_explicit_never_null(good):
    """A nested digest the execution did not produce is explicit and stops the case from
    qualifying -- a recorded send whose bytes were never snapshotted carries an unobserved form, a TQL that never
    reached the snapshot an unreadable one -- while a bare null digest is refused on write."""
    good._slt_inputs.record("upload", "op.jar")                     # a send with no snapshotted bytes
    doc = doc_of(good)
    [sent] = doc["inputs"]["renderedInputs"]
    assert sent["templateSha256"] == sent["renderedSha256"] == evidence.NO_BYTES_FORM
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is False and "not observed" in doc["run"]["qualifiesReason"]
    for key in ("templateSha256", "renderedSha256"):
        bad = doc_of(good)
        bad["inputs"]["renderedInputs"][0][key] = None
        rejected(bad, good, "evidence-invalid")
    bad = doc_of(good)
    bad["inputs"]["caseAssets"]["tqlSha256"] = None                 # every live case declares its TQL
    rejected(bad, good, "evidence-invalid")
    [tql] = [e for e in good._slt_inputs.entries.values() if e["role"] == "tql"]
    del good._slt_inputs.entries[str(Path(tql["path"]).resolve())]  # its bytes never reached the snapshot
    doc = doc_of(good)
    assert doc["inputs"]["caseAssets"]["tqlSha256"] == evidence.NO_TQL_FORM
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is False and "not observed or unreadable" in doc["run"]["qualifiesReason"]


def test_golden_integrity_is_recomputed_from_its_digests(good):
    """integrity.goldens covers exactly the case's goldens, both digests are observed values,
    and ``unchanged`` is recomputed from them -- the flag is never its own witness. A golden that could not be re-read
    carries the explicit unreadable form and the case does not qualify."""
    rel = "expected/tgt.csv"
    for mutate in (lambda d: d["integrity"]["goldens"][rel].update(inputSha256=None, finalSha256=None),
                   lambda d: d["integrity"]["goldens"][rel].update(finalSha256="sha256:" + "2" * 64),
                   lambda d: d["integrity"]["goldens"][rel].update(unchanged=False),
                   lambda d: d["integrity"]["goldens"][rel].update(inputSha256="sha256:" + "3" * 64,
                                                                  finalSha256="sha256:" + "3" * 64),
                   lambda d: d["integrity"]["goldens"].pop(rel)):
        bad = doc_of(good)
        mutate(bad)
        rejected(bad, good, "evidence-invalid")
    [gold] = [Path(e["path"]) for e in good._slt_inputs.entries.values() if e["role"] == "golden"]
    gold.unlink()
    doc = doc_of(good)
    assert doc["integrity"]["goldens"][rel] == {"inputSha256": doc["inputs"]["caseAssets"]["goldens"][rel],
                                                "finalSha256": {"unreadable": evidence.UNREADABLE_GOLDEN},
                                                "unchanged": False}
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is False and "golden changed during the run" in doc["run"]["qualifiesReason"]


def test_comparison_variant_must_agree_with_its_assertion_record(good):
    """The legitimate table-to-table diff -- its ``<route>:<schema.table>`` source and no
    template -- stays valid and qualifying, while a source that names no table it read, a data comparison re-typed as
    a diff, and a comparison no assertion record witnesses are refused on write."""
    comp = good._slt_data[0]
    diff = {**comp, "type": "diff", "expected": {**comp["expected"], "source": "postgres-source:qasource.t1_src",
                                                 "templateSha256": None}}
    good._slt_data, good._slt_records = [diff], [record(type="diff", spec=DIFF_SPEC)]
    good._slt_resources["owned"] = [SRC_OWNED]
    doc = doc_of(good)
    evidence.validate_case(doc)
    assert doc["run"]["qualifies"] is True, doc["run"]["qualifiesReason"]
    for source in ("unknown", "qasource.t1_src", "postgres-source:t1_src", "postgres-elsewhere:qasource.t1_src"):
        bad = doc_of(good)
        bad["data"]["comparisons"][0]["expected"]["source"] = source
        rejected(bad, good, "evidence-invalid")
    good._slt_records = [record()]                    # the run recorded a data assertion; the envelope claims a diff
    rejected(doc_of(good), good, "evidence-invalid")
    good._slt_data, good._slt_records = [comp], []    # a comparison no assertion record witnesses
    rejected(doc_of(good), good, "evidence-invalid")


def test_missing_input_hash_rejected_on_write_and_read(good):
    doc = doc_of(good)
    doc["inputs"]["caseAssets"]["manifestSha256"] = None
    rejected(doc, good, "evidence-invalid")
    path = write(good)
    tampered = json.loads(path.read_text())
    del tampered["inputs"]["logicalInputsSha256"]
    path.write_text(json.dumps(tampered))
    with pytest.raises(EvidenceError, match="logicalInputsSha256"):
        evidence.read(path)


# ---------------------------------------------------------------- validator and reader

def test_writer_rejects_partial_key(good):
    doc = doc_of(good)
    doc["partial"] = {"by": "legacy writer", "missing": ["data"]}
    rejected(doc, good, "evidence-partial")


def test_validator_recomputes_qualifies_and_rejects_mismatch(good):
    doc = doc_of(good)
    doc["data"]["comparisons"][0]["actual"]["owned"] = False            # a writer bug cannot claim green
    e = rejected(doc, good, "evidence-qualifies")
    assert "not owned" in str(e)


def test_unknown_key_rejected_on_write_and_read(good):
    doc = doc_of(good)
    doc["runtime"]["extra"] = 1
    rejected(doc, good, "evidence-invalid")
    path = write(good)
    tampered = json.loads(path.read_text())
    tampered["surprise"] = True
    path.write_text(json.dumps(tampered))
    with pytest.raises(EvidenceError, match="unknown"):
        evidence.read(path)


def test_read_path_identity_mismatch_rejected(good, tmp_path):
    path = write(good)
    other = tmp_path / "elsewhere" / "evidence" / "lc" / "20990101T000000Z-ffffffff" / "evidence.json"
    other.parent.mkdir(parents=True)
    shutil.copy(path, other)
    with pytest.raises(EvidenceError) as ei:
        evidence.read(other)
    assert ei.value.code == "evidence-path-mismatch"
    assert evidence.read(path).complete


def test_exclusive_write_never_overwrites_prior_envelope(good):
    path = write(good)
    before = path.read_bytes()
    with pytest.raises(EvidenceError) as ei:
        write(good)
    assert ei.value.code == "evidence-exists" and path.read_bytes() == before


def test_no_fresh_literal_and_reports_carry_invocation(good, monkeypatch):
    monkeypatch.setenv("SLT_INVOCATION_ID", "4f7c2a9e0b1d4c3e8a6f5b2d1c0e9f87")
    doc = doc_of(good)
    assert doc["reports"][0] == {"kind": "junit-xml", "path": good.config.option.xmlpath,
                                 "invocation": "4f7c2a9e0b1d4c3e8a6f5b2d1c0e9f87"}
    assert doc["run"]["invocation"] == "4f7c2a9e0b1d4c3e8a6f5b2d1c0e9f87"
    assert '"fresh"' not in json.dumps(doc) and '"fresh"' not in (REPO / "scripts/live/livetest/evidence.py").read_text()


def test_to_v1_projection_equals_sidecar_record(good):
    rep = report()
    _slt_collect_report(good.config, good, rep)
    path = evidence.finalize_from_report(good, rep)
    _slt_write_sidecar(good.config)
    sidecar = json.loads((Path(good.config.option.xmlpath).with_name("junit.slt.json")).read_text())["tests"][0]
    projected = evidence.to_v1(json.loads(path.read_text()))
    assert projected == {k: sidecar[k] for k in projected}


def test_data_aggregates_recomputable_from_comparisons(good):
    doc = doc_of(good)
    data = doc["data"]
    assert data["canonicalSha256"] == canon.aggregate([data["comparisons"][0]["actual"]["sha256"]])
    assert data["rowCount"] == 2 and data["profile"] == canon.PROFILE
    data["canonicalSha256"] = "sha256:" + "0" * 64
    rejected(doc, good, "evidence-invalid")


# ---------------------------------------------------------------- qualification

def test_qualifies_requires_canon_exact_owned_equal_and_goldens_unchanged(good):
    base = doc_of(good)
    view = evidence.exact_view(base)
    args = ("passed", base["lifecycle"], base["lifecycle"]["cleanup"], base["resources"])
    assert evidence.final_outcome(*args, exact=view)[1:] == (True, None)
    for mutate, why in ((lambda v: v["data"]["comparisons"][0].__setitem__("equal", False), "not equal"),
                        (lambda v: v["data"]["comparisons"][0]["actual"].__setitem__("owned", False), "not owned"),
                        (lambda v: v["integrity"]["goldens"]["expected/tgt.csv"].__setitem__("unchanged", False),
                         "golden changed during the run"),
                        (lambda v: v["assertions"][0].__setitem__("status", "failed"), "assertion data"),
                        (lambda v: v["integrity"]["evidenceErrors"].append("x"), "evidence errors")):
        v = json.loads(json.dumps(view))
        mutate(v)
        ok, reason = evidence.final_outcome(*args, exact=v)[1:]
        assert ok is False and why in reason, reason


def test_legacy_exact_diff_never_qualifies(good):
    legacy = {"index": None, "type": "diff", "target": "qatarget.t", "route": "postgres-target",
              "profile": canon.LEGACY_PROFILE, "equal": True, "actual": {"owned": False}}
    only = exactdata.data_section([legacy])
    assert only["profile"] == canon.LEGACY_PROFILE and only["canonicalSha256"] == canon.aggregate([])
    canon_entry = good._slt_data[0]
    good._slt_data = [legacy]
    doc = doc_of(good)
    assert doc["run"]["qualifies"] is False and doc["run"]["qualifiesReason"] == evidence.NO_DATA_REASON
    evidence.validate_case(doc)
    good._slt_data = [canon_entry, legacy]                                 # an equal canon comparison does not rescue it
    mixed = doc_of(good)
    assert mixed["run"]["qualifies"] is False and "legacy-text/1" in mixed["run"]["qualifiesReason"]
    assert mixed["data"]["canonicalSha256"] == canon.aggregate([canon_entry["actual"]["sha256"]])
    evidence.validate_case(mixed)


def test_smoke_only_lifecycle_case_does_not_qualify(good):
    good._slt_data = []
    doc = doc_of(good)
    assert doc["data"] == {"reason": evidence.NO_DATA_REASON}
    assert doc["run"]["qualifies"] is False and doc["run"]["qualifiesReason"] == evidence.NO_DATA_REASON
    evidence.validate_case(doc)


# ---------------------------------------------------------------- source identity

def test_source_identity_bounded_git_not_a_worktree_reason_timeout_unreadable(tmp_path):
    assert evidence.GIT_TIMEOUT_S == 5.0
    assert evidence.source_identity(tmp_path) == {"reason": "not-a-git-worktree"}      # real git, not a worktree
    calls = []

    def timeout(argv):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, evidence.GIT_TIMEOUT_S)
    assert evidence.source_identity(tmp_path, run=timeout) == {"unreadable": "git did not answer within 5s"}
    assert len(calls) == 1

    def ok(argv):
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout="6a141ee660836251476c3c8e8b7af217e1821191\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=" M a.txt\0?? b.txt\0", stderr="")
    assert evidence.source_identity(tmp_path, run=ok) == {
        "repo": str(tmp_path), "head": "6a141ee660836251476c3c8e8b7af217e1821191", "dirty": ["a.txt", "b.txt"]}
    many = lambda argv: SimpleNamespace(returncode=0, stdout="x\n" if "rev-parse" in argv else "".join(   # noqa: E731
        f" M f{i}\0" for i in range(evidence.DIRTY_CAP + 1)), stderr="")
    assert "unreadable" in evidence.source_identity(tmp_path, run=many)


# ---------------------------------------------------------------- static

_WRITE_ATTRS = {"write_text", "write_bytes", "replace", "rename", "move", "copy", "copy2", "copyfile", "copytree"}
# Hand-reviewed: the evidence modules' only writers. Every other writer in livetest/ and striim_test/ must not
# touch a match-derived path (checked below).
_ALLOWED_42 = {("scripts/live/livetest/evidence.py", "write_case_envelope"), ("scripts/live/livetest/evidence.py", "finalize_session"),
               ("scripts/live/livetest/evidence.py", "check"),          # increment 3: the run record, run-evidence.json
               ("scripts/live/livetest/evidence.py", "stage_on")}       # A private staged copy of a server file


def _writes(path: Path):
    tree = ast.parse(path.read_text())
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in _WRITE_ATTRS and not (name == "replace" and isinstance(f, ast.Attribute) and not (
                    isinstance(f.value, ast.Name) and f.value.id == "os")):
                yield fn, call
            elif name == "open" and len(call.args) + len(call.keywords) > 1:
                mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
                    yield fn, call
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os":
                    yield fn, call


def _code(fn) -> str:
    """The function's code without docstrings (prose saying a writer leaves ``expected/`` alone is not a write)."""
    import copy
    tree = copy.deepcopy(fn)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body[0] = ast.Pass()
    return ast.unparse(tree)


def test_no_write_path_to_goldens():
    hits = []
    for root in ("scripts/live/livetest", "scripts/cli/striim_test"):
        for path in sorted((REPO / root).rglob("*.py")):
            rel = str(path.relative_to(REPO))
            if "/_builtin/" in rel:
                continue
            for fn, _call in _writes(path):
                hits.append((rel, fn, _code(fn)))
    assert hits, "the scan found no writer at all; the detector is broken"
    for rel in ("scripts/live/livetest/canon.py", "scripts/live/livetest/exactdata.py", "scripts/live/livetest/inputs.py"):
        assert not [h for h in hits if h[0] == rel], f"{rel} must not write"
    assert {(rel, fn.name) for rel, fn, _ in hits if rel.endswith("/evidence.py")} <= _ALLOWED_42
    for rel, fn, src in hits:
        assert "['match']" not in src and ".golden(" not in src and "'expected/" not in src, (rel, fn.name)
    probe = ast.parse("def f(spec):\n    'expected/ prose'\n    return spec['match']\n").body[0]
    assert "['match']" in _code(probe) and 'expected/ prose' not in _code(probe)   # the detector itself



# ---------------------------------------------------------------- runtime.services

def _service(container, status):
    ext = {"reason": "external service"}
    img = ext if status == "external" else "example/img:1"
    return {"name": "svc", "container": container, "status": status, "image": img,
            "imageId": ext if status == "external" else "sha256:" + "b" * 64}


def test_an_external_service_with_no_container_is_valid(good):
    # A connection-only service (no compose file, no container) used through its existing-instance
    # setting: there is no container to name, as for a native Striim endpoint.
    doc = doc_of(good)
    doc["runtime"]["services"] = [_service(None, "external")]
    assert json.loads(write(good, doc).read_text())["runtime"]["services"][0]["container"] is None


def test_a_service_the_run_uses_in_docker_still_needs_its_container(good):
    for status in ("reused", "owned", "provisioned-and-kept"):
        doc = doc_of(good)
        doc["runtime"]["services"] = [_service(None, status)]
        assert "runtime.services[0] needs name, container and status" in str(rejected(doc, good, "evidence-invalid"))
