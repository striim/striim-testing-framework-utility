"""The Teradata JDBC driver: an opt-in image input (SLT_TERADATA_JDBC=1), never a required one."""
import hashlib
import json
import re
import types
from pathlib import Path

import pytest

from livetest import paths, striim_provision as sp
from inttest import paths as int_paths

JAR = sp.TERADATA_JDBC_JAR
STRIIM = Path(sp.__file__).resolve().parents[1] / "services" / "striim"
VERSION = sp._DEFAULT_STRIIM_VERSION


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(sp.TERADATA_JDBC_ENV, raising=False)
    monkeypatch.delenv(sp.DEPS_MANIFEST_ENV, raising=False)


def _deps(tmp_path):
    d = tmp_path / "images" / "striim" / "deps"
    d.mkdir(parents=True, exist_ok=True)
    for n in sp.REQUIRED_DEPS:
        (d / n).write_text("x")
    (tmp_path / "images" / "striim" / "files").mkdir(parents=True, exist_ok=True)
    (tmp_path / "images" / "striim" / "Dockerfile").write_text("FROM scratch\n")
    return d


def test_not_a_required_dep():
    assert JAR not in sp.REQUIRED_DEPS
    required_check = (STRIIM / "images" / "striim" / "Dockerfile").read_text().split("ERROR: missing")[0]
    assert JAR not in required_check


def test_dockerfile_copies_it_only_when_present():
    text = (STRIIM / "images" / "striim" / "Dockerfile").read_text()
    assert f"if [ -s /deps/{JAR} ]; then" in text


def test_download_script_fetches_it_only_on_opt_in():
    text = (STRIIM / "download-dependencies.sh").read_text()
    gate = re.search(r'if \[ "\$\{SLT_TERADATA_JDBC:-\}" = "1" \]; then\n(.*?)\nfi\n', text, re.S)
    assert gate and JAR in gate.group(1)
    assert text.count(JAR) == gate.group(1).count(JAR)


def test_ensure_deps_skips_when_required_present_and_not_opted_in(tmp_path):
    _deps(tmp_path)
    sp.ensure_deps(tmp_path, run=lambda *a, **k: pytest.fail("downloaded"))


def test_ensure_deps_fetches_when_opted_in_and_missing(tmp_path, monkeypatch):
    _deps(tmp_path)
    monkeypatch.setenv(sp.TERADATA_JDBC_ENV, "1")
    calls = []
    sp.ensure_deps(tmp_path, run=lambda argv, cwd=None: calls.append(argv))
    assert calls and "download-dependencies.sh" in " ".join(calls[0])


def test_ensure_deps_skips_when_opted_in_and_present(tmp_path, monkeypatch):
    (_deps(tmp_path) / JAR).write_text("jar")
    monkeypatch.setenv(sp.TERADATA_JDBC_ENV, "1")
    sp.ensure_deps(tmp_path, run=lambda *a, **k: pytest.fail("downloaded"))


def test_optional_jars_names_the_driver_and_extra_lib(tmp_path):
    d = _deps(tmp_path)
    (d / JAR).write_bytes(b"jar")
    (d / "extra-lib").mkdir()
    (d / "extra-lib" / "mine.jar").write_bytes(b"mine")
    (d / "extra-lib" / "README.md").write_text("not a jar")
    assert sp.optional_jars(tmp_path) == {JAR: hashlib.sha256(b"jar").hexdigest(),
                                          "extra-lib/mine.jar": hashlib.sha256(b"mine").hexdigest()}


def test_empty_driver_is_not_counted(tmp_path):
    (_deps(tmp_path) / JAR).write_bytes(b"")
    assert sp.optional_jars(tmp_path) == {}


def test_a_changed_jar_set_changes_the_build_inputs(tmp_path, monkeypatch):
    d = _deps(tmp_path)
    monkeypatch.setattr(sp, "image_present", lambda v, run=None: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda v, sd, run=None: True)
    current = lambda: sp.ensure_image(tmp_path, {"STRIIM_VERSION": VERSION},
                                      run=lambda *a, **k: pytest.fail("built"))
    current()
    before = sp._local_build_inputs(tmp_path)
    assert json.loads(before["files/" + sp.OPTIONAL_JARS_FILE]) == {"schemaVersion": 1, "sha256": {}}
    (d / JAR).write_bytes(b"jar")
    current()
    assert sp._local_build_inputs(tmp_path) != before
    (d / JAR).unlink()
    current()
    assert sp._local_build_inputs(tmp_path) == before


def _manifest(tmp_path, *, with_jar):
    root = tmp_path / "inputs"
    ddir = root / "deps"
    ddir.mkdir(parents=True)
    names = [*sp._required_deps(VERSION), *([JAR] if with_jar else [])]
    digests = {}
    for n in names:
        (ddir / n).write_bytes(n.encode())
        digests[n] = hashlib.sha256(n.encode()).hexdigest()
    m = root / "deps-manifest.json"
    m.write_text(json.dumps({"schemaVersion": 1, "directory": "deps", "sha256": digests}))
    return m


def test_manifest_stages_the_driver_on_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv(sp.TERADATA_JDBC_ENV, "1")
    staged = sp.stage_striim_deps(tmp_path / "ctx", VERSION, _manifest(tmp_path, with_jar=True))
    assert (staged / JAR).read_bytes() == JAR.encode()


def test_manifest_without_the_driver_refuses_on_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv(sp.TERADATA_JDBC_ENV, "1")
    with pytest.raises(sp.StriimDepsError, match=f"{sp.TERADATA_JDBC_ENV}=1 but .* names no {JAR}"):
        sp.stage_striim_deps(tmp_path / "ctx", VERSION, _manifest(tmp_path, with_jar=False))


def test_manifest_without_opt_in_never_stages_it_and_drops_a_stale_copy(tmp_path):
    ctx = tmp_path / "ctx"
    stale = sp._deps_dir(ctx) / JAR
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    staged = sp.stage_striim_deps(ctx, VERSION, _manifest(tmp_path, with_jar=True))
    assert not (staged / JAR).exists()


def _answer(rc):
    calls = []
    def run(argv):
        calls.append(argv)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    return run, calls


def test_missing_driver_names_the_setting():
    run, calls = _answer(1)
    why = sp.missing_service_jar("teradata", "docker", run=run)
    assert sp.TERADATA_JDBC_ENV + "=1" in why and JAR in why
    assert calls[0][:2] == ["docker", "exec"] and calls[0][-1] == f"/opt/striim/lib/{JAR}"


@pytest.mark.parametrize("service,mode,rc", [
    ("teradata", "docker", 0),       # present
    ("teradata", "docker", 125),     # container not reachable: the normal path reports it
    ("teradata", "native", 1),       # a native install's lib/ is the user's
    ("postgres", "docker", 1),       # needs no optional jar
])
def test_no_reason_otherwise(service, mode, rc):
    run, _ = _answer(rc)
    assert sp.missing_service_jar(service, mode, run=run) is None


def test_setting_is_read_from_dotenv():
    assert sp.TERADATA_JDBC_ENV in paths.SERVICE_KEYS
    assert sp.TERADATA_JDBC_ENV in int_paths.SERVICE_KEYS
