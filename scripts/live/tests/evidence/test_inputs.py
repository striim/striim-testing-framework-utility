"""livetest.inputs: the per-case input snapshot, driven by the current manifest loader with no plugin hooks.

The legacy repo tested the snapshot only through the evidence envelope (tests/evidence/test_envelope.py),
which needs the plugin hooks re-applied in a later change. These tests pin the snapshot on its own.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from livetest import inputs
from livetest.manifest import load_manifest

LIVE = Path(inputs.__file__).resolve().parents[1]


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _case(tmp_path, *, match=True, server_file=True):
    case = tmp_path / "case"
    (case / "expected").mkdir(parents=True)
    (case / "app.tql").write_text("CREATE APPLICATION a;\n")
    (case / "ddl_target.sql").write_text("CREATE TABLE t (id int);\n")
    (case / "seed.sql").write_text("INSERT INTO s VALUES (1);\n")
    (case / "f.csv").write_text("1\n")
    (case / "expected" / "t.csv").write_text("id\n1\n")
    body = ("name: c\ntql: app.tql\n"
            "ddl:\n  - {file: ddl_target.sql, db: postgres-target}\n"
            "seed:\n  - {file: seed.sql, db: postgres-source}\n")
    if server_file:
        body += "server_files:\n  - {file: f.csv, dest: /tmp/x/f.csv}\n"
    body += "assert:\n  smoke: true\n"
    if match:
        body += "  data:\n    - {target: s.t, target_db: postgres-target, match: expected/t.csv}\n"
    (case / "test.yaml").write_text(body)
    return case


def test_referenced_lists_every_input_once_in_manifest_order(tmp_path):
    case = _case(tmp_path)
    m = load_manifest(case / "test.yaml")
    got = [(role, Path(p).name) for role, p in inputs.referenced(m, case / "test.yaml")]
    assert got == [("manifest", "test.yaml"), ("tql", "app.tql"), ("ddl", "ddl_target.sql"),
                   ("seed", "seed.sql"), ("server-file", "f.csv"), ("golden", "t.csv")]


def test_snapshot_hashes_bytes_and_splits_assets(tmp_path):
    case = _case(tmp_path)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    assets = snap.case_assets()
    assert assets["manifestSha256"] == _sha((case / "test.yaml").read_bytes())
    assert assets["tqlSha256"] == _sha((case / "app.tql").read_bytes())
    assert assets["goldens"] == {"expected/t.csv": _sha((case / "expected" / "t.csv").read_bytes())}
    assert set(assets["files"]) == {"ddl_target.sql", "seed.sql", "f.csv"}
    assert snap.golden(case / "expected" / "t.csv") == b"id\n1\n"


def test_missing_input_fails_before_provisioning_naming_the_file(tmp_path):
    case = _case(tmp_path)
    (case / "seed.sql").unlink()
    with pytest.raises(inputs.InputSnapshotError, match=r"input-missing: seed .*seed\.sql"):
        inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")


def test_oversized_golden_is_refused(tmp_path, monkeypatch):
    case = _case(tmp_path)
    monkeypatch.setattr(inputs, "PER_FILE_CAP", 4)
    with pytest.raises(inputs.InputSnapshotError, match=r"input-too-large: golden .*t\.csv"):
        inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")


def test_total_cap_is_enforced(tmp_path, monkeypatch):
    case = _case(tmp_path)
    monkeypatch.setattr(inputs, "TOTAL_CAP", 4)
    with pytest.raises(inputs.InputSnapshotError, match="exceed 4 bytes"):
        inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")


def test_only_goldens_are_capped_and_kept(tmp_path, monkeypatch):
    """The caps bound the bytes an exact assertion parses; every other input is hashed and its bytes are not kept."""
    case = _case(tmp_path)
    monkeypatch.setattr(inputs, "PER_FILE_CAP", 5)
    monkeypatch.setattr(inputs, "TOTAL_CAP", 5)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    for e in snap.entries.values():
        assert e["sha256"] == "sha256:" + hashlib.sha256(Path(e["path"]).read_bytes()).hexdigest()
        assert (e["bytes"] is not None) == (e["role"] == "golden"), e
    assert snap.golden(case / "expected" / "t.csv") == b"id\n1\n"


def test_not_strict_leaves_a_missing_input_out_and_hashes_an_oversized_golden(tmp_path, monkeypatch):
    """A case without an exact: block: the snapshot never fails it, so it skips or fails where it did before."""
    case = _case(tmp_path)
    (case / "seed.sql").unlink()
    monkeypatch.setattr(inputs, "PER_FILE_CAP", 4)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml", strict=False)
    assert not [e for e in snap.entries.values() if Path(e["path"]).name == "seed.sql"]
    [gold] = [e for e in snap.entries.values() if e["role"] == "golden"]
    assert gold["bytes"] is None and gold["sha256"] == "sha256:" + hashlib.sha256(b"id\n1\n").hexdigest()


def test_lookup_of_an_unsnapshotted_file_is_refused(tmp_path):
    case = _case(tmp_path, match=False)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    with pytest.raises(inputs.InputSnapshotError, match="input-not-snapshotted"):
        snap.golden(case / "expected" / "t.csv")


def test_logical_sha_ignores_rendering_and_rendered_sha_tracks_it(tmp_path):
    case = _case(tmp_path)
    m = load_manifest(case / "test.yaml")
    a = inputs.snapshot(m, case / "test.yaml")
    b = inputs.snapshot(m, case / "test.yaml")
    a.record("tql", "app.tql", "CREATE APPLICATION ns1_a;\n", path=case / "app.tql")
    b.record("tql", "app.tql", "CREATE APPLICATION ns2_a;\n", path=case / "app.tql")
    assert a.logical_inputs_sha256() == b.logical_inputs_sha256()
    assert a.rendered_manifest_sha256() != b.rendered_manifest_sha256()


def test_record_counts_uses_and_keeps_the_template_digest(tmp_path):
    case = _case(tmp_path)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    for _ in range(2):
        snap.record("ddl", "ddl_target.sql", "CREATE TABLE ns_t (id int);\n", path=case / "ddl_target.sql")
    snap.record("upload", "op.jar", b"\x00jar")                      # verbatim bytes, no template file
    [ddl, upload] = snap.rendered_inputs()
    assert ddl == {"role": "ddl", "name": "ddl_target.sql",
                   "templateSha256": _sha((case / "ddl_target.sql").read_bytes()),
                   "renderedSha256": _sha(b"CREATE TABLE ns_t (id int);\n"), "uses": 2}
    assert upload["templateSha256"] == upload["renderedSha256"] == _sha(b"\x00jar")


def test_record_on_is_a_no_op_without_an_attached_snapshot(tmp_path):
    m = load_manifest(_case(tmp_path) / "test.yaml")
    inputs.record_on(m, "tql", "app.tql", "x")                       # no _slt_inputs attached: nothing happens
    snap = inputs.snapshot(m, m.dir / "test.yaml")
    m._slt_inputs = snap
    inputs.record_on(m, "tql", "app.tql", "x")
    assert len(snap.rendered_inputs()) == 1


def test_verify_goldens_detects_a_changed_golden(tmp_path):
    case = _case(tmp_path)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    [before] = snap.verify_goldens().values()
    assert before["unchanged"]
    (case / "expected" / "t.csv").write_text("id\n2\n")
    [after] = snap.verify_goldens().values()
    assert not after["unchanged"] and after["inputSha256"] == before["inputSha256"]


def test_snapshot_holds_only_referenced_roles(tmp_path):
    case = _case(tmp_path, match=False, server_file=False)
    snap = inputs.snapshot(load_manifest(case / "test.yaml"), case / "test.yaml")
    assert {e["role"] for e in snap.entries.values()} == {"manifest", "tql", "ddl", "seed"}
    assert snap.goldens() == {}


@pytest.mark.parametrize("manifest_path", sorted((LIVE / "regression").rglob("test.yaml")),
                         ids=lambda p: str(p.parent.relative_to(LIVE / "regression")))
def test_every_framework_case_snapshots(manifest_path):
    # The framework's own cases (hello, framework, services) reference only files that exist.
    m = load_manifest(manifest_path)
    snap = inputs.snapshot(m, manifest_path)
    assert snap.case_assets()["manifestSha256"] == _sha(manifest_path.read_bytes())
