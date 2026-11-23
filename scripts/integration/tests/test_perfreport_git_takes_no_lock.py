"""`collect_git_info` reads the user's checkout and must never write its index (FW-112).

A plain `git status` rewrites a stale `.git/index` under `.git/index.lock`; killed by the report's
timeout it leaves the lock behind. The live tier's copy of this test is
scripts/live/tests/evidence/test_git_reads_take_no_lock.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from inttest import perfreport

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, **_ENV})


def _index_stamp(repo: Path) -> tuple[int, int]:
    st = (repo / ".git" / "index").stat()
    return st.st_mtime_ns, st.st_ino


def _stale_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    (path / "f.txt").write_text("x\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    later = (path / "f.txt").stat().st_mtime + 100
    os.utime(path / "f.txt", (later, later))
    return path


def test_collect_git_info_does_not_write_the_index(tmp_path, monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    template = _stale_repo(tmp_path / "template")
    control = tmp_path / "control"
    shutil.copytree(template, control, copy_function=shutil.copy2)
    before = _index_stamp(control)
    subprocess.run(["git", "-C", str(control), "status", "--porcelain"], check=True, capture_output=True)
    assert _index_stamp(control) != before, "control: a plain git status must rewrite a stale index"

    repo = tmp_path / "repo"
    shutil.copytree(template, repo, copy_function=shutil.copy2)
    before = _index_stamp(repo)
    got = perfreport.collect_git_info(repo)
    assert got["commit"] and got["dirty"] is False, got
    assert _index_stamp(repo) == before
    assert not (repo / ".git" / "index.lock").exists()
