"""Hermetic tests for the Striim installer-input gate.

No Docker. ``SLT_STRIIM_DEPS_MANIFEST`` names a JSON manifest (``schemaVersion`` 1,
``directory`` relative to the file, ``sha256`` per required filename). Parsing,
verification and staging are filesystem + env contracts; the ``ensure_deps`` /
``ensure_image`` paths run with recording runners, so a download or build that must not
happen fails the test instead of launching anything.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from livetest import striim_provision as sp

VERSION = sp._DEFAULT_STRIIM_VERSION
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "striim-deps"


@pytest.fixture(autouse=True)
def _no_ambient_manifest(monkeypatch):
    monkeypatch.delenv("SLT_STRIIM_DEPS_MANIFEST", raising=False)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _inputs(tmp_path: Path, *, directory: str = "deps", drop=(), salt: bytes = b"", **fields):
    """A consumer-supplied input set: every required file + a matching manifest.
    ``salt`` makes a distinct but equally valid installer set. Returns
    (manifest_path, deps_dir, required_names)."""
    root = tmp_path / "inputs"
    ddir = root / directory
    ddir.mkdir(parents=True)
    names = sp._required_deps(VERSION)
    digests = {}
    for n in names:
        payload = b"payload:" + salt + n.encode()
        (ddir / n).write_bytes(payload)
        digests[n] = _sha(payload)
    for n in drop:
        digests.pop(n)
    doc = {"schemaVersion": 1, "directory": directory, "sha256": digests}
    doc.update(fields)
    manifest = root / "deps-manifest.json"
    manifest.write_text(json.dumps(doc))
    return manifest, ddir, names


def _rewrite(manifest: Path, **changes):
    doc = json.loads(manifest.read_text())
    doc.update(changes)
    manifest.write_text(json.dumps(doc))


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))


def _trap(argv, cwd=None):
    pytest.fail(f"must not run {argv!r}")


def _image_absent(argv):
    return SimpleNamespace(stdout="", returncode=0)


# ---------------------------------------------------------------------------
# load_striim_deps_manifest
# ---------------------------------------------------------------------------

def test_valid_manifest_verifies_every_required_file(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    got = sp.verify_striim_deps(manifest, VERSION)
    assert sorted(got) == sorted(names)
    assert all(p.parent == ddir.resolve() for p in got.values())


def test_env_variable_names_the_manifest(tmp_path, monkeypatch):
    manifest, _, names = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    assert sorted(sp.verify_striim_deps(version=VERSION)) == sorted(names)


def test_missing_variable_refuses_naming_it():
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.load_striim_deps_manifest(env={})


@pytest.mark.parametrize("make", ["absent", "directory"])
def test_unreadable_manifest_refuses(tmp_path, make):
    path = tmp_path / "manifest.json"
    if make == "directory":
        path.mkdir()
    with pytest.raises(sp.StriimDepsError, match="cannot read Striim deps manifest"):
        sp.load_striim_deps_manifest(path)


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", '"a string"'])
def test_malformed_manifest_refuses(tmp_path, text):
    path = tmp_path / "manifest.json"
    path.write_text(text)
    with pytest.raises(sp.StriimDepsError, match="not valid JSON|must be a JSON object"):
        sp.load_striim_deps_manifest(path)


@pytest.mark.parametrize("version", [2, 0, "1", True, 1.0, None])
def test_wrong_schema_version_refuses(tmp_path, version):
    manifest, _, _ = _inputs(tmp_path)
    _rewrite(manifest, schemaVersion=version)
    with pytest.raises(sp.StriimDepsError, match="unsupported schemaVersion"):
        sp.load_striim_deps_manifest(manifest)


def test_unknown_field_refuses(tmp_path):
    manifest, _, _ = _inputs(tmp_path)
    _rewrite(manifest, url="https://example.invalid/deps")
    with pytest.raises(sp.StriimDepsError, match="unknown field"):
        sp.load_striim_deps_manifest(manifest)


def test_directory_escaping_with_dotdot_refuses(tmp_path):
    manifest, _, _ = _inputs(tmp_path)
    (tmp_path / "outside").mkdir()
    _rewrite(manifest, directory="../outside")
    with pytest.raises(sp.StriimDepsError, match="escapes the manifest directory"):
        sp.load_striim_deps_manifest(manifest)


def test_directory_escaping_through_symlink_refuses(tmp_path):
    manifest, ddir, _ = _inputs(tmp_path)
    outside = tmp_path / "outside"
    shutil.move(str(ddir), str(outside))
    ddir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(sp.StriimDepsError, match="escapes the manifest directory"):
        sp.load_striim_deps_manifest(manifest)


def test_absolute_directory_refuses(tmp_path):
    manifest, ddir, _ = _inputs(tmp_path)
    _rewrite(manifest, directory=str(ddir))
    with pytest.raises(sp.StriimDepsError, match="relative to the manifest file"):
        sp.load_striim_deps_manifest(manifest)


def test_non_hex_digest_and_path_key_refuse(tmp_path):
    manifest, _, names = _inputs(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["sha256"][names[0]] = "not-a-digest"
    manifest.write_text(json.dumps(doc))
    with pytest.raises(sp.StriimDepsError, match="not a 64-hex digest"):
        sp.load_striim_deps_manifest(manifest)
    doc["sha256"] = {"../escape.deb": "0" * 64}
    manifest.write_text(json.dumps(doc))
    with pytest.raises(sp.StriimDepsError, match="not a plain filename"):
        sp.load_striim_deps_manifest(manifest)


def test_contract_fixtures():
    directory, expected = sp.load_striim_deps_manifest(FIXTURES / "striim-deps-manifest.valid.json")
    assert directory == FIXTURES.resolve()
    assert len(expected) == 2
    with pytest.raises(sp.StriimDepsError, match="escapes the manifest directory"):
        sp.load_striim_deps_manifest(FIXTURES / "striim-deps-manifest.invalid-escaping-directory.json")


# ---------------------------------------------------------------------------
# verify_striim_deps — file-level policy
# ---------------------------------------------------------------------------

def test_missing_file_refuses_naming_it(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / names[0]).unlink()
    with pytest.raises(sp.StriimDepsError) as e:
        sp.verify_striim_deps(manifest, VERSION)
    assert f"missing Striim build dependency: {names[0]}" in str(e.value)


def test_digest_mismatch_refuses_naming_it(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / names[1]).write_bytes(b"CORRUPTED PAYLOAD")
    with pytest.raises(sp.StriimDepsError) as e:
        sp.verify_striim_deps(manifest, VERSION)
    assert f"digest mismatch for Striim build dependency: {names[1]}" in str(e.value)
    assert _sha(b"CORRUPTED PAYLOAD") not in str(e.value)


def test_required_file_on_disk_but_absent_from_manifest_refuses(tmp_path):
    manifest, ddir, names = _inputs(tmp_path, drop=[sp._required_deps(VERSION)[2]])
    assert (ddir / names[2]).is_file()
    with pytest.raises(sp.StriimDepsError, match=f"no expected digest for Striim build dependency: {names[2]}"):
        sp.verify_striim_deps(manifest, VERSION)


def test_file_symlink_escaping_directory_refuses(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    outside = tmp_path / "elsewhere.bin"
    outside.write_bytes((ddir / names[0]).read_bytes())
    (ddir / names[0]).unlink()
    (ddir / names[0]).symlink_to(outside)
    with pytest.raises(sp.StriimDepsError, match="symlink escaping the deps directory"):
        sp.verify_striim_deps(manifest, VERSION)


# ---------------------------------------------------------------------------
# stage_striim_deps — only verified, required, listed bytes reach the build context
# ---------------------------------------------------------------------------

def test_unlisted_files_are_never_staged(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / "extra-not-in-manifest.bin").write_bytes(b"x")
    doc = json.loads(manifest.read_text())
    (ddir / "listed-but-not-required.bin").write_bytes(b"y")
    doc["sha256"]["listed-but-not-required.bin"] = _sha(b"y")
    manifest.write_text(json.dumps(doc))
    staged = sp.stage_striim_deps(tmp_path / "ctx", VERSION, manifest)
    assert sorted(p.name for p in staged.iterdir()) == sorted(names)


def test_nothing_is_copied_when_verification_fails(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / names[-1]).write_bytes(b"WRONG")
    ctx = tmp_path / "ctx"
    with pytest.raises(sp.StriimDepsError):
        sp.stage_striim_deps(ctx, VERSION, manifest)
    deps = sp._deps_dir(ctx)
    assert not deps.exists() or not any(deps.iterdir())


def test_staged_bytes_are_verified(tmp_path, monkeypatch):
    manifest, _, names = _inputs(tmp_path)
    monkeypatch.setattr(shutil, "copyfile", lambda src, dst: Path(dst).write_bytes(b"torn"))
    ctx = tmp_path / "ctx"
    with pytest.raises(sp.StriimDepsError, match="staged Striim build dependency"):
        sp.stage_striim_deps(ctx, VERSION, manifest)
    assert not any(sp._deps_dir(ctx).iterdir())


def test_verified_installer_copy_changed_is_restaged(tmp_path):
    manifest, ddir, names = _inputs(tmp_path)
    ctx = tmp_path / "ctx"
    staged = sp.stage_striim_deps(ctx, VERSION, manifest)
    (staged / names[0]).write_bytes(b"tampered after staging")
    sp.stage_striim_deps(ctx, VERSION, manifest)
    assert (staged / names[0]).read_bytes() == (ddir / names[0]).read_bytes()


# ---------------------------------------------------------------------------
# ensure_deps / ensure_image — command construction without Docker
# ---------------------------------------------------------------------------

def test_candidate_deps_verified_before_build(tmp_path, monkeypatch):
    manifest, _, names = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    ctx = tmp_path / "ctx"
    sp.ensure_deps(ctx, run=_trap)                     # stages; never downloads
    assert sorted(p.name for p in sp._deps_dir(ctx).iterdir()) == sorted(names)
    rec = _Recorder()
    sp.ensure_image(ctx, run=rec, query_run=_image_absent)
    assert rec.calls == [(["docker", "compose", "build", "slt-striim"], str(ctx))]


def test_missing_installer_blocks_build(tmp_path, monkeypatch):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / names[0]).unlink()
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    with pytest.raises(sp.StriimDepsError, match=names[0]):
        sp.ensure_image(tmp_path / "ctx", run=_trap, query_run=_image_absent)


def test_wrong_installer_hash_blocks_image_reuse(tmp_path, monkeypatch):
    manifest, ddir, names = _inputs(tmp_path)
    (ddir / names[0]).write_bytes(b"WRONG")
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    monkeypatch.setattr(sp, "image_present", lambda *a, **k: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda *a, **k: True)
    with pytest.raises(sp.StriimDepsError, match="digest mismatch"):
        sp.ensure_image(tmp_path / "ctx", run=_trap, query_run=_image_absent)


def test_installed_tree_without_manifest_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_deps(tmp_path / "ctx", run=_trap)
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_image(tmp_path / "ctx", run=_trap, query_run=_image_absent)


def test_checkout_without_manifest_keeps_legacy_download(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: True)
    rec = _Recorder()
    sp.ensure_deps(tmp_path / "ctx", run=rec)
    assert rec.calls == [(["./download-dependencies.sh"], str(tmp_path / "ctx"))]


# ---------------------------------------------------------------------------
# Positive control: the build recipe resolves from the layout
# ---------------------------------------------------------------------------

def test_build_recipe_resolves_from_layout():
    # The build RECIPE (Dockerfile + files/**) is in the service tree. The deps/ payloads are
    # not tracked (a checkout may have downloaded them), so they are not checked here.
    recipe = Path(sp.__file__).resolve().parents[1] / "services" / "striim" / "images" / "striim"
    assert (recipe / "Dockerfile").is_file()
    assert any(p.is_file() for p in (recipe / "files").rglob("*"))


# ---------------------------------------------------------------------------
# Review round 1, finding 2: an installed tree never trusts build-context deps
# ---------------------------------------------------------------------------

def _populate_context(ctx: Path, payload: bytes = b"unverified:") -> None:
    d = sp._deps_dir(ctx)
    d.mkdir(parents=True, exist_ok=True)
    for n in sp._required_deps(VERSION):
        (d / n).write_bytes(payload + n.encode())


def test_installed_populated_context_without_manifest_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    ctx = tmp_path / "ctx"
    _populate_context(ctx)
    assert sp.deps_present(sp._deps_dir(ctx), VERSION)          # the old shortcut would pass
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_deps(ctx, run=_trap)
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_image(ctx, run=_trap, query_run=_image_absent)
    monkeypatch.setattr(sp, "image_present", lambda *a, **k: True)
    monkeypatch.setattr(sp, "image_inputs_match", lambda *a, **k: True)
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_image(ctx, run=_trap, query_run=_image_absent)   # no reuse shortcut either


def test_previously_staged_file_corrupted_is_never_built(tmp_path, monkeypatch):
    manifest, ddir, names = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    ctx = tmp_path / "ctx"
    sp.ensure_deps(ctx, run=_trap)
    staged = sp._deps_dir(ctx)
    (staged / names[0]).write_bytes(b"corrupted after staging")
    seen = {}

    def build(argv, cwd=None):
        seen["bytes"] = (staged / names[0]).read_bytes()

    sp.ensure_image(ctx, run=build, query_run=_image_absent)
    assert seen["bytes"] == (ddir / names[0]).read_bytes()          # re-verified + restaged
    # without the manifest, the (again) corrupted staged copy is refused in an installed tree
    (staged / names[0]).write_bytes(b"corrupted again")
    monkeypatch.delenv("SLT_STRIIM_DEPS_MANIFEST")
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    with pytest.raises(sp.StriimDepsError, match="SLT_STRIIM_DEPS_MANIFEST is not set"):
        sp.ensure_image(ctx, run=_trap, query_run=_image_absent)


# ---------------------------------------------------------------------------
# Review round 1, finding 3: image reuse is bound to the verified installer set
# ---------------------------------------------------------------------------

def _recipe(tmp_path: Path) -> Path:
    ctx = tmp_path / "ctx"
    r = ctx / "images" / "striim"
    (r / "files").mkdir(parents=True)
    (r / "Dockerfile").write_text("FROM x\nCOPY ./files /slt-build-inputs/files\n")
    (r / "files" / "entrypoint.sh").write_text("#!/bin/sh\n")
    return ctx


class _FakeDocker:
    """Records builds and answers image_inputs_match's docker calls: the 'image' carries a
    copy of the build inputs (Dockerfile + files/) present when it was built."""

    def __init__(self, ctx: Path):
        self.ctx = ctx
        self.builds = 0
        self.embedded = None
        self.create_mode = "ok"

    def build(self, argv, cwd=None):
        assert argv == ["docker", "compose", "build", "slt-striim"] and cwd == str(self.ctx)
        self.builds += 1
        self.embedded = sp._local_build_inputs(self.ctx)

    def query(self, argv):
        verb = argv[1]
        if verb == "images":
            return SimpleNamespace(stdout="img\n" if self.embedded is not None else "", returncode=0)
        if verb == "rm":
            return SimpleNamespace(stdout="", returncode=0)
        if verb == "create":
            if self.create_mode == "oserror":
                raise OSError("docker create failed")
            if self.create_mode == "nonzero":
                return SimpleNamespace(stdout="", returncode=125)
            if self.create_mode == "empty":
                return SimpleNamespace(stdout="", returncode=0)
            return SimpleNamespace(stdout="cid\n", returncode=0)
        if verb == "cp":
            dest = Path(argv[3])
            for rel, data in self.embedded.items():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                (dest / rel).write_bytes(data)
            return SimpleNamespace(stdout="", returncode=0)
        pytest.fail(f"unexpected docker call {argv!r}")


def test_image_reuse_is_bound_to_the_verified_installer_set(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    ctx = _recipe(tmp_path)
    docker = _FakeDocker(ctx)
    manifest_a, _, _ = _inputs(tmp_path / "a", salt=b"A")
    manifest_b, _, names = _inputs(tmp_path / "b", salt=b"B")

    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest_a))
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1                                       # same verified set: reuse

    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest_b))  # valid, same version, other bytes
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 2                                       # must rebuild, never reuse A
    record = json.loads((ctx / "images/striim/files" / sp.INSTALLER_IDENTITY_FILE).read_text())
    _, expected_b = sp.load_striim_deps_manifest(manifest_b)
    assert record["sha256"] == {n: expected_b[n] for n in names}

    # an image whose build inputs carry no recorded installer identity is not a match
    docker.embedded = {k: v for k, v in docker.embedded.items()
                       if not k.endswith(sp.INSTALLER_IDENTITY_FILE)}
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 3


def test_checkout_legacy_build_keeps_presence_check_and_drops_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: True)
    ctx = _recipe(tmp_path)
    _populate_context(ctx)
    stale = ctx / "images/striim/files" / sp.INSTALLER_IDENTITY_FILE
    stale.write_text("stale\n")
    rec = _Recorder()
    sp.ensure_image(ctx, run=rec, query_run=_image_absent)
    assert rec.calls == [(["docker", "compose", "build", "slt-striim"], str(ctx))]
    assert not stale.exists()


# ---------------------------------------------------------------------------
# Review round 2, R2-1: a failed image inspection is never a match in candidate mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["nonzero", "empty", "oserror"])
def test_failed_inspection_is_not_a_match_in_candidate_mode(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(sp, "_in_checkout", lambda: False)
    ctx = _recipe(tmp_path)
    docker = _FakeDocker(ctx)
    manifest_a, _, _ = _inputs(tmp_path / "a", salt=b"A")
    manifest_b, _, _ = _inputs(tmp_path / "b", salt=b"B")
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest_a))
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1                                       # image A present
    docker.create_mode = failure                                    # inspection now fails
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest_b))  # valid, different set B
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 2                                       # rebuilt, never reused A


def test_legacy_checkout_inspection_failure_keeps_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_in_checkout", lambda: True)
    ctx = _recipe(tmp_path)
    _populate_context(ctx)
    docker = _FakeDocker(ctx)
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1
    docker.create_mode = "nonzero"
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 1                                       # legacy policy unchanged


# ---------------------------------------------------------------------------
# A checkout is anything outside site-packages; nothing set keeps today's behaviour
# ---------------------------------------------------------------------------

def test_this_checkout_is_a_checkout_and_site_packages_is_not(monkeypatch):
    assert sp._in_checkout() is True
    assert sp.candidate_mode() is False
    monkeypatch.setattr(sp, "__file__", "/usr/lib/python3/site-packages/livetest/striim_provision.py")
    assert sp._in_checkout() is False
    assert sp.candidate_mode() is True


def test_nothing_set_downloads_missing_deps_as_before(tmp_path):
    rec = _Recorder()
    sp.ensure_deps(tmp_path / "ctx", run=rec)
    assert rec.calls == [(["./download-dependencies.sh"], str(tmp_path / "ctx"))]


def test_nothing_set_reuses_a_matching_image_and_writes_no_identity(tmp_path):
    ctx = _recipe(tmp_path)
    docker = _FakeDocker(ctx)
    docker.embedded = sp._local_build_inputs(ctx)          # an image built from these inputs
    sp.ensure_image(ctx, run=docker.build, query_run=docker.query)
    assert docker.builds == 0
    assert not (ctx / "images/striim/files" / sp.INSTALLER_IDENTITY_FILE).exists()
    assert sp.verify_current_inputs(ctx) is False


# ---------------------------------------------------------------------------
# The identity writer never follows a redirect (on a copy of the checkout's recipe)
# ---------------------------------------------------------------------------

def _checkout_recipe_copy(tmp_path: Path) -> Path:
    ctx = tmp_path / "ctx"
    src = Path(sp.__file__).resolve().parents[1] / "services" / "striim" / "images" / "striim"
    shutil.copytree(src, ctx / "images" / "striim", ignore=shutil.ignore_patterns("deps"))
    return ctx


def _tree_state(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_identity_writer_refuses_a_redirected_files_directory(tmp_path, monkeypatch):
    ctx = _checkout_recipe_copy(tmp_path)
    files = ctx / "images" / "striim" / "files"
    outside = tmp_path / "outside"
    shutil.copytree(files, outside)
    shutil.rmtree(files)
    files.symlink_to(outside, target_is_directory=True)
    before = _tree_state(outside)
    manifest, _, _ = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    with pytest.raises(sp.StriimDepsError, match="is a symlink"):
        sp.ensure_deps(ctx, run=_trap)
    with pytest.raises(sp.StriimDepsError, match="is a symlink"):
        sp.ensure_image(ctx, run=_trap, query_run=_image_absent)
    assert _tree_state(outside) == before


def test_identity_writer_never_follows_a_planted_temp_link(tmp_path, monkeypatch):
    ctx = _checkout_recipe_copy(tmp_path)
    files = ctx / "images" / "striim" / "files"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("untouched\n")
    manifest, _, _ = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    monkeypatch.setattr(os, "urandom", lambda n: b"\x00" * n)
    planted = files / f".{sp.INSTALLER_IDENTITY_FILE}.tmp-{os.getpid()}-{'00' * 4}"
    planted.symlink_to(sentinel)
    with pytest.raises(sp.StriimDepsError, match="already exists"):
        sp.ensure_deps(ctx, run=_trap)
    assert sentinel.read_text() == "untouched\n"
    assert planted.is_symlink()
    assert not (files / sp.INSTALLER_IDENTITY_FILE).exists()


# ---------------------------------------------------------------------------
# Staged installers never reach git; a matching staged copy is not re-hashed at the source
# ---------------------------------------------------------------------------

def test_staged_installers_are_git_ignored():
    import subprocess
    repo = Path(sp.__file__).resolve().parents[3]
    if not (repo / ".git").exists():
        pytest.skip("no .git here (a git archive copy)")
    deps = sp._deps_dir(Path(sp.__file__).resolve().parents[1] / "services" / "striim")
    for name in sp._required_deps(VERSION):
        r = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", str(deps / name)])
        assert r.returncode == 0, f"{name} in deps/ is not git-ignored"
    r = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", str(deps / ".gitignore")])
    assert r.returncode == 1, "deps/.gitignore itself must stay tracked"


def test_matching_staged_copy_skips_the_source_hash(tmp_path, monkeypatch):
    manifest, ddir, names = _inputs(tmp_path)
    monkeypatch.setenv("SLT_STRIIM_DEPS_MANIFEST", str(manifest))
    ctx = tmp_path / "ctx"
    sp.ensure_deps(ctx, run=_trap)
    hashed = []
    real = sp._sha256_of
    monkeypatch.setattr(sp, "_sha256_of", lambda p: hashed.append(Path(p)) or real(p))
    sp.ensure_deps(ctx, run=_trap)
    staged = sp._deps_dir(ctx)
    assert sorted(p.name for p in hashed) == sorted(names)            # each file hashed once
    assert all(p.parent == staged for p in hashed)                    # the staged copy, not the source
    (staged / names[0]).write_bytes(b"corrupted")                     # the staged check stays
    hashed.clear()
    sp.ensure_deps(ctx, run=_trap)
    assert (ddir / names[0]) in hashed                                # source re-verified for the restage
    assert (staged / names[0]).read_bytes() == (ddir / names[0]).read_bytes()
