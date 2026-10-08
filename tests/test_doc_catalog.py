"""The documentation index must match the files readers can browse."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_committed_catalog_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/doc_catalog.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def catalog(root, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/doc_catalog.py"), "--root", str(root), *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def doc_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("# First steps\n\nA guide to running your first framework tests. More detail.\n")
    return tmp_path


@pytest.mark.parametrize("change", ["add", "edit", "delete", "catalog"])
def test_check_detects_drift_without_writing(doc_repo, change):
    assert catalog(doc_repo).returncode == 0
    target = doc_repo / "docs/CATALOG.md"
    original = target.read_bytes()
    if change == "add":
        (doc_repo / "new.md").write_text("# New guide\n\nAnother guide.\n")
    elif change == "edit":
        (doc_repo / "README.md").write_text("# First steps\n\nUpdated instructions.\n")
    elif change == "delete":
        (doc_repo / "README.md").unlink()
    else:
        target.write_bytes(original + b"stale\n")
    before = target.read_bytes()
    checked = catalog(doc_repo, "--check")
    assert checked.returncode == 1 and "stale" in checked.stdout
    assert target.read_bytes() == before
    assert catalog(doc_repo).returncode == 0
    assert catalog(doc_repo, "--check").returncode == 0
    regenerated = target.read_bytes()
    assert catalog(doc_repo).returncode == 0
    assert target.read_bytes() == regenerated


def test_scope_and_descriptions(doc_repo):
    (doc_repo / ".gitignore").write_text("scratch/\n")
    for name in ("vendor/lib/README.md", "build/README.md", "tests/fixtures/README.md",
                 "cases/expected/README.md", "scratch/README.md", "RELEASE-NOTES.md"):
        path = doc_repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Excluded\n\nExcluded content.\n")
    guide = doc_repo / "samples/a guide/README.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("# A sample\n\n<!-- annotation -->\n```\nnot prose\n```\n\n"
                     "Use [the guide](../../README.md) with `test.yaml`.\n")
    assert catalog(doc_repo, "--check").returncode == 1
    assert catalog(doc_repo).returncode == 0
    text = (doc_repo / "docs/CATALOG.md").read_text()
    assert "Excluded content" not in text and "More detail" not in text
    assert "../samples/a%20guide/README.md" in text
    assert "Use the guide with test.yaml." in text
    assert text.index("## Writing tests") < text.index("[A sample]") < text.index("## Services")
