"""Hook-free envelope tests ported unchanged from the legacy repo's tests/evidence/test_envelope.py: the
envelope read path, the 4.1 partial marker, the observed-image rule and input hashing. The rest of that
file needs the manifest exact:/lifecycle: blocks and the plugin hooks, and comes with a later change."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import canon, evidence, infra, inputs
from livetest.evidence import EvidenceError
from livetest.manifest import load_manifest

RUN = "20260915T101500Z-abcd1234"


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


def test_every_referenced_input_hashed_including_goldens_outside_expected(tmp_path):
    case = _case(tmp_path, "name: lc\npurpose: p\ntql: app.tql\nrequires: [postgres]\n"
                           "ddl:\n  - file: ddl.sql\n    db: postgres-target\n"
                           "seed:\n  - file: seed.sql\n    db: postgres-target\n"
                           "server_files:\n  - {file: in.csv, dest: /tmp/in.csv}\n"
                           "assert:\n  data:\n    - {target: qatarget.t, target_db: postgres-target, match: goldens/g.csv}\n")
    (case / "seed.sql").write_text("INSERT INTO ${TID}tgt VALUES (1);\n")
    (case / "in.csv").write_text("x\n")
    (case / "goldens").mkdir()
    (case / "goldens" / "g.csv").write_text("id\n1\n")
    (case / "expected" / "unreferenced.csv").write_text("id\n9\n")
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    sha = lambda rel: "sha256:" + hashlib.sha256((case / rel).read_bytes()).hexdigest()   # noqa: E731
    assert snap.case_assets() == {"manifestSha256": sha("test.yaml"), "tqlSha256": sha("app.tql"),
                                  "goldens": {"goldens/g.csv": sha("goldens/g.csv")},
                                  "files": {"ddl.sql": sha("ddl.sql"), "seed.sql": sha("seed.sql"), "in.csv": sha("in.csv")}}
    (case / "in.csv").unlink()
    with pytest.raises(inputs.InputSnapshotError, match="input-missing: server-file"):
        inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")


def test_blockless_server_file_over_16_mib_is_hashed_not_refused(tmp_path):
    """A case without an exact: block and a 17 MiB server file (a shaded jar, say) snapshots as it ran before 3.2:
    the file is hashed in chunks, never refused and never held in memory. Sparse, so nothing large is written."""
    case = tmp_path / "c"
    case.mkdir()
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\n")
    (case / "test.yaml").write_text("name: big\ntql: app.tql\nrequires: [postgres]\n"
                                    "server_files:\n  - {file: fat-udf.jar, dest: fat-udf.jar, when: pre_deploy}\n"
                                    "assert:\n  smoke: true\n")
    size = 17 * 1024 * 1024
    with open(case / "fat-udf.jar", "wb") as f:
        f.truncate(size)
    m = load_manifest(case / "test.yaml")
    assert m.exact is None
    snap = inputs.snapshot(m, case / "test.yaml", strict=m.exact is not None)
    [jar] = [e for e in snap.entries.values() if e["role"] == "server-file"]
    assert jar["bytes"] is None and jar["sha256"] == "sha256:" + hashlib.sha256(bytes(size)).hexdigest()
    assert snap.case_assets()["files"] == {"fat-udf.jar": jar["sha256"]}


def test_read_rejects_version_3_and_a_v1_sidecar(tmp_path):
    p = tmp_path / "evidence.json"
    p.write_text(json.dumps({"evidenceVersion": 3}))
    with pytest.raises(EvidenceError) as ei:
        evidence.read(p)
    assert ei.value.code == "unsupported-evidence-version"
    p.write_text(json.dumps({"schema_version": 1, "tests": []}))
    with pytest.raises(EvidenceError) as ei:
        evidence.read(p)
    assert ei.value.code == "unsupported-evidence" and "no provenance" in str(ei.value)


def test_read_4_1_partial_is_incomplete_never_qualifies(tmp_path):
    run = {"runId": RUN, "attempt": "0a1b2c3d", "caseId": "live:cases/lc::lc", "suite": "cases/lc", "tier": "live",
           "startedAt": "t", "endedAt": "t", "status": "passed", "skipReason": None, "failure": None,
           "qualifies": True, "qualifiesReason": None}
    doc = {"evidenceVersion": 2, "partial": {"by": "legacy writer", "missing": evidence.PARTIAL_MISSING_41}, "run": run,
           "inputs": {}, "runtime": {}, "lifecycle": {"ready": None, "completion": None, "cleanup": {"status": "ok"}},
           "assertions": [], "reports": [], "review": None,
           "resources": {"infrastructure": None, "owned": [], "reused": [], "foreign": [], "cleanupVerified": True,
                         "verificationGaps": []}}
    path = tmp_path / "evidence" / "lc" / RUN / "evidence.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc))
    env = evidence.read(path)
    assert env.complete is False and env.qualifies is False


def test_image_tag_is_not_the_observed_runtime_version(monkeypatch):
    """The image reference is image metadata; the version is what the running runtime answers."""
    monkeypatch.setattr(infra, "inspect_image", lambda _c: {"imageId": "sha256:" + "1" * 64, "image": "slt-striim:latest"})
    monkeypatch.setattr(infra, "_docker_inspect", lambda argv: SimpleNamespace(
        returncode=0, stdout=f"Platform-5.4.2.jar\n{infra.RUNTIME_PROBE_MARK}\n5.4.2\n", stderr=""))
    cfg = SimpleNamespace(_slt_release={"STRIIM_VERSION": "5.4.2"}, _slt_striim=SimpleNamespace(mode="docker", url="http://localhost:9080"))
    observed = infra.observe_striim(cfg)["observed"]
    assert observed["image"] == "slt-striim:latest" and observed["imageId"] == "sha256:" + "1" * 64
    assert observed["version"] == "5.4.2"
    view = {"data": {}, "integrity": {}, "assertions": [], "blocked": evidence.forms({"runtime": {"striim": {"observed": observed}}}),
            "striim": {"expected": cfg._slt_release, "observed": observed}}
    assert evidence._exact_reason({**view, "data": {"comparisons": [{"profile": canon.PROFILE, "equal": True,
                                                                     "actual": {"owned": True}}]}}) is None
