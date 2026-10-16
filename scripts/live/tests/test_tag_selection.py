"""Manifest tags select live cases through pytest's real collection filters."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests import _hermetic_child


@pytest.fixture
def collect_tags(tmp_path):
    for name, tags in {"alpha": ["p1", "slow-path"], "bravo": ["p10"],
                       "charlie": None, "delta": ["p2"]}.items():
        folder = tmp_path / name
        folder.mkdir()
        text = f"name: {name}\ntql: app.tql\nassert: {{smoke: true}}\n"
        if tags is not None:
            text += "tags: " + json.dumps(tags) + "\n"
        (folder / "test.yaml").write_text(text)
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    live: live case\n")
    (tmp_path / "conftest.py").write_text('''
import json

def pytest_collection_finish(session):
    selected = {item.name: sorted(mark.name for mark in item.iter_markers())
                for item in session.items}
    print("TAG_SELECTION=" + json.dumps(selected, sort_keys=True))
''')

    def collect(*args):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "livetest.plugin", "--collect-only",
             "--strict-markers", "-W", "error::pytest.PytestUnknownMarkWarning", "-q", *args],
            cwd=tmp_path, env=_hermetic_child.child_env(tmp_path / "missing"),
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        line = next(line for line in result.stdout.splitlines() if line.startswith("TAG_SELECTION="))
        return json.loads(line.split("=", 1)[1])

    return collect


@pytest.mark.parametrize(("args", "expected"), [
    (("-k", "p1"), {"alpha", "bravo"}),
    (("-m", "tag_p1"), {"alpha"}),
    (("-m", "tag_p10"), {"bravo"}),
    (("-m", "tag_slow_path"), {"alpha"}),
    (("-k", "slow-path"), {"alpha"}),
    (("-m", "not tag_p1 and not tag_p10 and not tag_p2"), {"charlie"}),
])
def test_tag_filters(collect_tags, args, expected):
    assert set(collect_tags(*args)) == expected


def test_tags_are_item_local_and_registered(collect_tags):
    assert collect_tags() == {
        "alpha": ["live", "tag_p1", "tag_slow_path"],
        "bravo": ["live", "tag_p10"],
        "charlie": ["live"],
        "delta": ["live", "tag_p2"],
    }
