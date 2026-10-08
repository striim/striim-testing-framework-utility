"""livetest.evidence identities that need no plugin hooks: the consumer root, the source identity and the
framework identity. The envelope tests that drive the report hook come with a later change."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import livetest
from livetest import evidence, layout, paths, project

_MANIFEST = "schemaVersion: 1\ntargets: []\nsuites:\n  live: .\n"


@pytest.fixture(autouse=True)
def _no_active_project():
    project._ACTIVE = None
    layout._reset()
    yield
    project._ACTIVE = None
    layout._reset()


def _consumer(tmp_path) -> Path:
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "gold-targets.yaml").write_text(_MANIFEST)
    return root


def test_consumer_root_is_the_project_manifest_dir(tmp_path, monkeypatch):
    root = _consumer(tmp_path)
    monkeypatch.setenv("GOLD_TARGETS", str(root / "gold-targets.yaml"))
    assert evidence.consumer_root(SimpleNamespace(rootpath=Path("/elsewhere"))) == root.resolve()


def test_consumer_root_is_the_active_project_without_gold_targets(tmp_path, monkeypatch):
    # striim-test --targets activates the project in the tier child; GOLD_TARGETS need not be set.
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    root = _consumer(tmp_path)
    project.load_and_activate(root / "gold-targets.yaml")
    assert evidence.consumer_root(SimpleNamespace(rootpath=tmp_path)) == root.resolve()


def test_consumer_root_resolves_a_relative_gold_targets(tmp_path, monkeypatch):
    root = _consumer(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("GOLD_TARGETS", "gold-targets.yaml")
    assert evidence.consumer_root(SimpleNamespace(rootpath=tmp_path)) == root.resolve()


def test_consumer_root_with_gold_targets_set_but_missing_raises_naming_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLD_TARGETS", str(tmp_path / "nope" / "gold-targets.yaml"))
    with pytest.raises(paths.PathConfigError, match="GOLD_TARGETS"):
        evidence.consumer_root(SimpleNamespace(rootpath=tmp_path))


def test_consumer_root_without_a_manifest_is_the_project_root(tmp_path, monkeypatch):
    # Not pytest's rootdir (scripts/live): the project whose cases run, which a customer names with
    # SLT_PROJECT_ROOT and which defaults to this checkout.
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    config = SimpleNamespace(rootpath=Path(livetest.__file__).resolve().parents[1])
    assert evidence.consumer_root(config) != config.rootpath
    assert evidence.consumer_root(config) == Path(livetest.__file__).resolve().parents[3]
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path))
    assert evidence.consumer_root(config) == tmp_path.resolve()


def test_consumer_root_with_a_missing_project_root_raises_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    monkeypatch.setenv("SLT_PROJECT_ROOT", str(tmp_path / "nope"))
    with pytest.raises(paths.PathConfigError, match="SLT_PROJECT_ROOT"):
        evidence.consumer_root(SimpleNamespace(rootpath=tmp_path))


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(root), "PATH": "/usr/bin:/bin"})


def test_source_identity_reads_head_and_dirty_paths(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "a")
    (tmp_path / "b.txt").write_text("b\n")
    ident = evidence.source_identity(tmp_path)
    assert ident["repo"] == str(tmp_path) and len(ident["head"]) == 40
    assert ident["dirty"] == ["b.txt"]


def test_source_identity_outside_git_is_a_reason(tmp_path):
    assert evidence.source_identity(tmp_path) == {"reason": "not-a-git-worktree"}


def test_source_identity_timeout_is_unreadable(tmp_path):
    def run(argv):
        raise subprocess.TimeoutExpired(argv, evidence.GIT_TIMEOUT_S)
    assert "unreadable" in evidence.source_identity(tmp_path, run=run)


def test_framework_identity_of_a_clone_carries_no_wheel_digests():
    # Wheels are retired: a clone has no wheel or lock to digest, so both stay null.
    ident = evidence.framework_identity(livetest, env={})
    assert ident == {"mode": "unknown", "provenance": getattr(livetest, "__provenance__", None),
                     "importPath": str(Path(livetest.__file__).parent), "wheelSha256": None, "lockSha256": None}
