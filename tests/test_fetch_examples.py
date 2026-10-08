"""Fetch transport parity, integrity checks and native-only safety, all hermetic."""
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import types
import zipfile

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/cli"))
from striim_test.fetch import fetch_bundle
from striim_test.errors import CliError

CASE = "exampleudf-rename"
BASE = f"cases/{CASE}/"


def _objects(change=None, extra=None):
    test = {"name": CASE, "tql": "app.tql", "requires": ["postgres"],
            "ddl": [{"file": "ddl.sql", "db": "postgres-source"}],
            "assert": {"smoke": True, "data": [{"match": "expected/out.csv"}]},
            "server_files": [{"file": "example.jar", "dest": "example.jar", "when": "pre_deploy", "load": "udf"}]}
    files = {"gold-targets.yaml": b"schemaVersion: 1\ntargets: []\nsuites:\n  live: cases\nstateDir: .state\n",
             BASE + "test.yaml": yaml.safe_dump(test).encode(), BASE + "example.jar": b"published jar",
             BASE + "app.tql": b"CREATE APPLICATION ${APP};", BASE + "ddl.sql": b"-- ${TID}",
             BASE + "expected/out.csv": b"result\n", "README.md": b"read me"}
    meta = {"schemaVersion": 1, "module": "ExampleUdf", "striimVersion": "5.4.2",
            "frameworkPin": "a" * 40, "caseIds": [CASE], "caseCount": 1,
            "jar": BASE + "example.jar", "jarSha256": hashlib.sha256(files[BASE + "example.jar"]).hexdigest(),
            "files": {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}}
    if change:
        change(files, meta)
    files["bundle.json"] = json.dumps(meta).encode()
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        for p, data in files.items():
            archive.writestr(p, data)
        if extra:
            archive.writestr(*extra)
    content = blob.getvalue()
    manifest = {"module": "ExampleUdf", "striimVersion": "5.4.2", "testBundleRelease": "5.4.2",
                "frameworkPin": "a" * 40, "caseIds": [CASE], "caseCount": 1,
                "jar": "example.jar", "jarSha256": hashlib.sha256(b"published jar").hexdigest(),
                "testBundle": "example.zip", "testBundleSha256": hashlib.sha256(content).hexdigest()}
    return {"manifest.json": json.dumps(manifest).encode(), "example.zip": content}


def test_real_and_emulator_transports_stage_identical_bytes(tmp_path, monkeypatch):
    objects = _objects()
    calls = []
    def gcloud(argv, **kw):
        calls.append(argv)
        assert argv[:3] == ["gcloud", "storage", "cat"]
        return objects[argv[-1].rsplit("/", 1)[-1]]
    monkeypatch.setattr(subprocess, "check_output", gcloud)
    fake = types.ModuleType("google.cloud.storage")
    def client(**kw):
        from google.auth.credentials import AnonymousCredentials
        assert isinstance(kw["credentials"], AnonymousCredentials)
        assert kw["client_options"] == {"api_endpoint": "http://localhost:4443"}
        return SimpleNamespace(bucket=lambda bucket: SimpleNamespace(blob=lambda path:
            SimpleNamespace(download_as_bytes=lambda: objects[path.rsplit("/", 1)[-1]])))
    fake.Client = client
    import google.cloud
    monkeypatch.setattr(google.cloud, "storage", fake, raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake)
    real, emulator = tmp_path / "real", tmp_path / "emulator"
    fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", real)
    fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", emulator, "http://localhost:4443")
    assert len(calls) == 2
    assert {str(p.relative_to(real)): p.read_bytes() for p in real.rglob("*") if p.is_file()} == {
        str(p.relative_to(emulator)): p.read_bytes() for p in emulator.rglob("*") if p.is_file()}
    from livetest import manifest, project
    project.load_and_activate(real / "gold-targets.yaml")
    assert manifest.load_manifest(real / BASE / "test.yaml").source_dir == real / BASE.rstrip("/")


@pytest.mark.parametrize("field,value", [("testBundleSha256", "0" * 64), ("jarSha256", "0" * 64),
                                       ("frameworkPin", "b" * 40), ("striimVersion", "5.4.0"),
                                       ("caseCount", 2), ("testBundle", "../outside.zip")])
def test_manifest_corruption_leaves_no_destination(tmp_path, field, value):
    objects = _objects()
    manifest = json.loads(objects["manifest.json"])
    manifest[field] = value
    objects["manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(CliError):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/windows", "a\\b", "a//b", "a/./b"])
def test_archive_paths_refused(tmp_path, path):
    objects = _objects(extra=(path, b"bad"))
    with pytest.raises(CliError, match="unsafe"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)
    assert not (tmp_path / "out").exists()


def test_symlink_archive_refused(tmp_path):
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    objects = _objects(extra=(link, b"/tmp/outside"))
    with pytest.raises(CliError, match="regular"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)


@pytest.mark.parametrize("kind", ["missing", "corrupt", "missing-inventory", "missing-dependency", "bad-json", "bad-pin"])
def test_payload_corruption_refused(tmp_path, kind):
    def change(files, meta):
        golden = BASE + "expected/out.csv"
        if kind == "missing":
            files.pop(golden)
        elif kind == "corrupt":
            files[golden] = b"corrupt"
        elif kind == "missing-inventory":
            meta["files"].pop(golden)
        elif kind == "missing-dependency":
            files.pop(golden)
            meta["files"].pop(golden)
        elif kind == "bad-json":
            files["gold-targets.yaml"] = b": invalid: ["
            meta["files"]["gold-targets.yaml"] = hashlib.sha256(files["gold-targets.yaml"]).hexdigest()
        else:
            meta["frameworkPin"] = "not a sha"
    objects = _objects(change)
    with pytest.raises(CliError):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)
    assert not (tmp_path / "out").exists()


def test_nonempty_destination_refused_before_download(tmp_path):
    (tmp_path / "keep").write_text("keep")
    with pytest.raises(CliError, match="empty"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path, read=lambda _: pytest.fail("must not download"))
    assert (tmp_path / "keep").read_text() == "keep"


def test_empty_destination_is_supported(tmp_path):
    objects = _objects()
    fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path, read=objects.__getitem__)
    assert (tmp_path / "bundle.json").is_file()


def test_fetch_parser_and_exit_code(tmp_path, monkeypatch):
    from striim_test import cli, _bootstrap
    monkeypatch.setattr(_bootstrap, "check_provenance", lambda: None)
    assert cli.main(["fetch", "--gcs-prefix", "not-gcs", "--destination", str(tmp_path)]) == 2
    assert cli.build_parser().parse_args(["fetch", "--gcs-prefix", "gs://b/t/r", "--destination", "out"]).endpoint is None


def test_native_only_never_provisions_unreachable_striim(monkeypatch, tmp_path):
    try:
        import striim_api
    except ImportError:
        monkeypatch.setitem(sys.modules, "striim_api", types.ModuleType("striim_api"))
    from livetest import plugin
    monkeypatch.setenv("SLT_STRIIM_NATIVE_ONLY", "1")
    monkeypatch.setattr(plugin, "probe_reachable", lambda *a: False)
    monkeypatch.setattr(plugin.time if hasattr(plugin, "time") else __import__("time"), "sleep", lambda _: None)
    monkeypatch.setattr(plugin, "_cluster_provision_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(plugin._slt_infra, "before_cluster_resolution", lambda *a: None)
    monkeypatch.setattr(plugin, "_striim5_running", lambda: pytest.fail("must not inspect Docker"))
    monkeypatch.setattr(plugin._sp, "ensure_deps", lambda *a, **k: pytest.fail("must not provision"))
    with pytest.raises(RuntimeError, match="Docker provisioning disabled"):
        plugin._resolve_striim(SimpleNamespace())


def test_symlink_destination_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(CliError, match="empty"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", link, read=lambda _: pytest.fail("must not download"))


def test_duplicate_archive_entry_refused(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate"):
        objects = _objects(extra=("README.md", b"duplicate"))
    with pytest.raises(CliError, match="duplicate"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)


def test_wrong_prefix_release_refused(tmp_path):
    objects = _objects()
    with pytest.raises(CliError, match="release mismatch"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.0", tmp_path / "out", read=objects.__getitem__)


def test_emulator_failure_is_a_cli_error(tmp_path, monkeypatch):
    import google.cloud
    from google.api_core.exceptions import NotFound
    fake = types.ModuleType("google.cloud.storage")
    def fail():
        raise NotFound("not seeded")
    fake.Client = lambda **kw: SimpleNamespace(bucket=lambda _: SimpleNamespace(blob=lambda _:
        SimpleNamespace(download_as_bytes=fail)))
    monkeypatch.setattr(google.cloud, "storage", fake, raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake)
    with pytest.raises(CliError, match="emulator download failed"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", "http://localhost:4443")


@pytest.mark.parametrize("module", ["OtherUdf", "", None, "../x"])
def test_module_must_name_the_prefix_module(tmp_path, module):
    def change(files, meta):
        meta["module"] = module
    objects = _objects(change)
    with pytest.raises(CliError, match="module mismatch"):
        fetch_bundle("gs://test-bucket/ExampleUdf/5.4.2", tmp_path / "out", read=objects.__getitem__)
