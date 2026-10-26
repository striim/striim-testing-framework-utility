"""op.upload names stay inside UploadedFiles/, teardown deletes only this case's uploads, and agent
jar placement fails loudly."""

import types
from pathlib import Path

import pytest

from livetest import opartifacts, plugin
from livetest.manifest import ManifestError, load_manifest


def _manifest(tmp_path, to):
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nop:\n  jar: java/OpenProcessors/FooOp\n"
                 f"  upload:\n    - {{from: cfg.json, to: '{to}'}}\nassert:\n  smoke: true\n")
    return p


@pytest.mark.parametrize("to", ["../cfg.json", "sub/cfg.json", "..", "/opt/striim/x.json", "a\\\\b.json"])
def test_upload_to_outside_uploaded_files_is_refused(tmp_path, to):
    with pytest.raises(ManifestError, match="must be a file name in UploadedFiles"):
        load_manifest(_manifest(tmp_path, to))


def test_upload_to_plain_name_is_accepted(tmp_path):
    assert load_manifest(_manifest(tmp_path, "${NS}-cfg.json")).op_uploads == [
        {"from": "cfg.json", "to": "${NS}-cfg.json"}]


def test_rendered_upload_name_is_checked_too():
    with pytest.raises(ValueError, match="not a file name in UploadedFiles"):
        plugin._upload_dest_name("cfg.json", "${DIR}cfg.json", "t1_", {"DIR": "../"})


def test_teardown_deletes_only_run_scoped_uploads():
    tokens = {"TID": "t0123456789_", "TID_UPPER": "T0123456789_", "NS": "SLT_x_t0123456789"}
    names = ["t0123456789_cfg.json", "SLT_x_t0123456789-cfg.json", "customers_lookup.json"]
    assert plugin._run_scoped_uploads(names, tokens) == names[:2]


def test_delete_artifacts_native_removes_only_the_named_file(tmp_path, monkeypatch):
    up = tmp_path / "UploadedFiles"; up.mkdir()
    (up / "x.json").write_text("mine"); (up / "y.json").write_text("other")
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    opartifacts.delete_artifacts(types.SimpleNamespace(mode="native"), ["x.json"])
    assert sorted(p.name for p in up.iterdir()) == ["y.json"]


def test_delete_artifacts_refuses_a_name_outside_uploaded_files(tmp_path, monkeypatch):
    (tmp_path / "keep.json").write_text("x")
    (tmp_path / "UploadedFiles").mkdir()
    monkeypatch.setenv("STRIIM_HOME", str(tmp_path))
    calls = []
    with pytest.raises(opartifacts.OpArtifactError, match="outside UploadedFiles"):
        opartifacts.delete_artifacts(types.SimpleNamespace(mode="docker"), ["../keep.json"],
                                     run=lambda argv: calls.append(argv))
    with pytest.raises(opartifacts.OpArtifactError):
        opartifacts.delete_artifacts(types.SimpleNamespace(mode="native"), ["../keep.json"])
    assert calls == [] and (tmp_path / "keep.json").exists()


def _agent_run(calls, fail):
    def run(argv):
        calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "sha256sum" in argv:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        rc = 1 if fail(argv) else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="boom" if rc else "")
    return run


def test_place_on_agent_fails_when_the_copy_fails(tmp_path):
    f = tmp_path / "Op-5.4.jar"; f.write_bytes(b"jar")
    calls = []
    with pytest.raises(opartifacts.OpArtifactError, match="docker cp.*boom"):
        opartifacts.place_on_agent(types.SimpleNamespace(mode="docker"), [f],
                                   run=_agent_run(calls, lambda a: a[:2] == ["docker", "cp"]))
    assert not [c for c in calls if c[:2] == ["docker", "restart"]]
    assert not [c for c in calls if "mv" in c]
    assert any("rm" in c and c[-1].endswith(".tmp." + str(__import__("os").getpid())) for c in calls)


def test_place_on_agent_fails_when_the_restart_fails(tmp_path):
    f = tmp_path / "Op-5.4.jar"; f.write_bytes(b"jar")
    with pytest.raises(opartifacts.OpArtifactError, match="restarting"):
        opartifacts.place_on_agent(types.SimpleNamespace(mode="docker"), [f],
                                   run=_agent_run([], lambda a: a[:2] == ["docker", "restart"]))
