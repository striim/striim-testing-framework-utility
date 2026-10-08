"""Materialization writes only to declared state; resources stay read-only.

Ported from the legacy framework repo. Hermetic: every workload subprocess is trapped.

Not here: the installed-mode striim build context (``striim_build_context``) and the
installer-identity writer tests. The build context is wheel-only and not ported; the
identity writer comes with the Striim deps gate.
"""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

from livetest import resource_profiles as rp


@pytest.fixture(autouse=True)
def _no_workloads(monkeypatch):
    def trap(*args, **kwargs):
        pytest.fail(f"workload subprocess attempted: {args[:1]!r}")
    monkeypatch.setattr(subprocess, "run", trap)
    monkeypatch.setattr(subprocess, "Popen", trap)


def _origin(tmp_path: Path, marker: str = "v1") -> Path:
    root = tmp_path / "pkg" / "services"
    files = {
        "demo/service.yaml": ("name: demo\nisolation: none\ncompose: compose.yaml\n", 0o644),
        "demo/compose.yaml": ("services:\n  demo:\n    build: ./images/demo\n", 0o644),
        "demo/images/demo/Dockerfile": ("FROM x\nCOPY run.sh /run.sh\n", 0o644),
        "demo/images/demo/run.sh": (f"#!/bin/sh\necho {marker}\n", 0o755),
        "demo/.gitignore": ("scratch\n", 0o644),
    }
    for rel, (text, mode) in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        os.chmod(p, mode)
    return root


def _tree_state(base: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            data = p.read_bytes() if stat.S_ISREG(st.st_mode) else b""
            out[p.relative_to(base).as_posix()] = (stat.S_IMODE(st.st_mode), hashlib.sha256(data).hexdigest())
    return out


@pytest.fixture
def readonly(request):
    locked: list[Path] = []

    def lock(tree: Path):
        for dirpath, dirnames, filenames in os.walk(tree, topdown=False):
            for n in filenames:
                p = Path(dirpath) / n
                os.chmod(p, stat.S_IMODE(p.stat().st_mode) & ~0o222)
            os.chmod(dirpath, 0o555)
        locked.append(tree)

    yield lock
    for tree in locked:
        for dirpath, dirnames, filenames in os.walk(tree):
            os.chmod(dirpath, 0o755)
            for n in filenames:
                p = Path(dirpath) / n
                os.chmod(p, stat.S_IMODE(p.stat().st_mode) | 0o200)


def test_readonly_materialization_write_boundary(tmp_path, readonly):
    root = _origin(tmp_path)
    readonly(root)
    prof = rp.select_profile("live", "demo", services_dir=root)
    state = tmp_path / "state"
    before = _tree_state(tmp_path)

    ptr = rp.materialize_profile(prof, state_dir=state, prefix="")

    after = _tree_state(tmp_path)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed and all(k == "state" or k.startswith("state/") for k in changed)
    assert ptr == state.resolve() / "services" / "live" / "default" / "demo"
    assert ptr.is_symlink()
    for rel, mode, sha in prof.assets:
        copied = ptr / rel
        assert stat.S_IMODE(copied.stat().st_mode) == mode
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == sha
    assert os.access(ptr / "images/demo/run.sh", os.X_OK)


def test_materialization_is_content_keyed_and_idempotent(tmp_path):
    root = _origin(tmp_path)
    state = tmp_path / "state"
    first = rp.materialize_profile(rp.select_profile("live", "demo", services_dir=root),
                                   state_dir=state)
    cas1 = Path(os.readlink(first))
    again = rp.materialize_profile(rp.select_profile("live", "demo", services_dir=root),
                                   state_dir=state)
    assert again == first and Path(os.readlink(again)) == cas1
    (root / "demo/images/demo/run.sh").write_text("#!/bin/sh\necho v2\n")
    moved = rp.materialize_profile(rp.select_profile("live", "demo", services_dir=root),
                                   state_dir=state)
    assert moved == first and Path(os.readlink(moved)) != cas1
    assert "v2" in (moved / "images/demo/run.sh").read_text()


def test_sequential_consumers_resource_state(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    a, b = tmp_path / "state-a", tmp_path / "state-b"
    pa = rp.materialize_profile(prof, state_dir=a)
    snapshot_a = _tree_state(a)
    pb = rp.materialize_profile(prof, state_dir=b)
    assert pa.parent != pb.parent
    assert a.resolve() in pa.parents and b.resolve() in pb.parents
    assert _tree_state(a) == snapshot_a


def test_prefix_workspaces_and_cleanup(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    state = tmp_path / "state"
    alpha = rp.materialize_profile(prof, state_dir=state, prefix="alpha")
    beta = rp.materialize_profile(prof, state_dir=state, prefix="beta")
    assert alpha != beta
    sentinel = alpha.parent / "foreign.txt"
    sentinel.write_text("not ours\n")
    foreign_link = alpha.parent / "foreign-link"
    foreign_link.symlink_to(tmp_path)

    removed = rp.discard_workspaces(state_dir=state, tier="live", prefix="alpha")

    assert removed
    assert not alpha.is_symlink() and not (alpha.parent / ".cas").exists()
    assert sentinel.read_text() == "not ours\n" and foreign_link.is_symlink()
    assert (beta / "service.yaml").is_file()
    assert rp.discard_workspaces(state_dir=state, tier="integration", prefix="alpha") == []


def test_failed_materialization_leaves_no_partial_pointer(tmp_path, monkeypatch):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    real = rp._copy_asset
    calls = []

    def dying(src, dst, mode):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        real(src, dst, mode)

    monkeypatch.setattr(rp, "_copy_asset", dying)
    state = tmp_path / "state"
    with pytest.raises(OSError, match="disk full"):
        rp.materialize_profile(prof, state_dir=state)
    base = state.resolve() / "services" / "live" / "default"
    assert not (base / "demo").exists() and not (base / "demo").is_symlink()
    assert not any((base / ".cas").glob("demo-*")) if (base / ".cas").exists() else True
    assert not any(p.name.startswith(".tmp-") for p in base.iterdir())


def test_origin_changed_after_selection_is_refused(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    (root / "demo/compose.yaml").write_text("services:\n  demo:\n    build: ./images/demo\n# x\n")
    with pytest.raises(rp.ProfileError, match="changed after it was selected"):
        rp.materialize_profile(prof, state_dir=tmp_path / "state")


def test_state_inside_resources_and_bad_prefix_refused(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    with pytest.raises(rp.ProfileError, match="inside the service resources"):
        rp.materialize_profile(prof, state_dir=root / "demo" / "state")
    with pytest.raises(rp.ProfileError, match="invalid stack prefix"):
        rp.materialize_profile(prof, state_dir=tmp_path / "state", prefix="Bad Prefix")


# ---------------------------------------------------------------------------
# Review round 1, finding 1: redirected workspace components are never followed
# ---------------------------------------------------------------------------

def test_intermediate_symlink_redirect_refused_for_materialization(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = tmp_path / "state"
    (state / "services" / "live").mkdir(parents=True)
    (state / "services" / "live" / "review").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(rp.ProfileError, match="is a symlink"):
        rp.materialize_profile(prof, state_dir=state, prefix="review")
    assert list(elsewhere.iterdir()) == []

    # a redirected services/ component pointing INTO the package: refused, package untouched
    state2 = tmp_path / "state2"
    state2.mkdir()
    (state2 / "services").symlink_to(root, target_is_directory=True)
    before = _tree_state(root)
    with pytest.raises(rp.ProfileError, match="is a symlink"):
        rp.materialize_profile(prof, state_dir=state2)
    assert _tree_state(root) == before


def test_redirected_content_store_and_lock_refused(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    state = tmp_path / "state"
    base = state / "services" / "live" / "default"
    base.mkdir(parents=True)
    (base / ".cas").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(rp.ProfileError, match="content store"):
        rp.materialize_profile(prof, state_dir=state)
    assert list(elsewhere.iterdir()) == []
    (base / ".cas").unlink()
    # the refused call above already took (and, on Unix, left behind) the workspace lock file
    (base / ".profile-workspace.lock").unlink(missing_ok=True)
    (base / ".profile-workspace.lock").symlink_to(elsewhere / "lock")
    with pytest.raises(rp.ProfileError, match="workspace lock"):
        rp.materialize_profile(prof, state_dir=state)
    assert list(elsewhere.iterdir()) == []


def test_cleanup_never_follows_redirects(tmp_path):
    root = _origin(tmp_path)
    prof = rp.select_profile("live", "demo", services_dir=root)
    victim_ptr = rp.materialize_profile(prof, state_dir=tmp_path / "victim", prefix="victim")
    victim_base = victim_ptr.parent
    snapshot = _tree_state(victim_base)

    state = tmp_path / "state"
    (state / "services" / "live").mkdir(parents=True)
    (state / "services" / "live" / "review").symlink_to(victim_base, target_is_directory=True)
    with pytest.raises(rp.ProfileError, match="is a symlink"):
        rp.discard_workspaces(state_dir=state, tier="live", prefix="review")

    other = tmp_path / "other" / "services" / "live" / "default"
    other.mkdir(parents=True)
    (other / ".cas").symlink_to(victim_base / ".cas", target_is_directory=True)
    with pytest.raises(rp.ProfileError, match="content store"):
        rp.discard_workspaces(state_dir=tmp_path / "other", tier="live")

    assert _tree_state(victim_base) == snapshot and victim_ptr.is_symlink()
