"""The engine reads the user's checkout with git and must never write its index (FW-112).

`git status` refreshes stale stat data and writes `.git/index` back, taking `.git/index.lock` to do it.
The engine bounds its git calls with a timeout; a git killed by that timeout while holding the lock
leaves it behind, and the user's next commit fails. A read with `--no-optional-locks` never takes it.

Each test makes the index stale (a tracked file's mtime moves, its content does not), proves that a
plain `git status` would rewrite the index (the control), then runs the engine's read on a fresh copy
and asserts the index was not written.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from livetest import caserecord, evidence

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **_ENV})


def _stale_repo(path: Path) -> Path:
    """A committed repo whose index holds stat data older than the file: a plain status rewrites it."""
    path.mkdir()
    _git(path, "init", "-q")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    later = (path / "f.txt").stat().st_mtime + 100
    os.utime(path / "f.txt", (later, later))
    return path


def _index_stamp(repo: Path) -> tuple[int, int]:
    st = (repo / ".git" / "index").stat()
    return st.st_mtime_ns, st.st_ino


@pytest.fixture
def stale(tmp_path, monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    template = _stale_repo(tmp_path / "template")
    control = tmp_path / "control"
    shutil.copytree(template, control, copy_function=shutil.copy2)
    before = _index_stamp(control)
    subprocess.run(["git", "-C", str(control), "status", "--porcelain"], check=True, capture_output=True)
    assert _index_stamp(control) != before, "control: a plain git status must rewrite a stale index"

    def fresh(name: str) -> Path:
        repo = tmp_path / name
        shutil.copytree(template, repo, copy_function=shutil.copy2)
        return repo
    return fresh


def test_source_identity_does_not_write_the_index(stale):
    repo = stale("evidence")
    before = _index_stamp(repo)
    got = evidence.source_identity(repo)
    assert got.get("head") and got.get("dirty") == [], got
    assert _index_stamp(repo) == before
    assert not (repo / ".git" / "index.lock").exists()


def test_git_state_does_not_write_the_index(stale):
    repo = stale("caserecord")
    before = _index_stamp(repo)
    got = caserecord.git_state(repo)
    assert got.get("head") and got.get("dirty") == [], got
    assert _index_stamp(repo) == before
    assert not (repo / ".git" / "index.lock").exists()
