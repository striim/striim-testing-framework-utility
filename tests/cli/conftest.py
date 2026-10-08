import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _clikit import copy_fixture, make_trap, runnable_clone  # noqa: E402


@pytest.fixture
def elsewhere(tmp_path):
    """An invocation directory unrelated to every repository and to the consumer."""
    d = tmp_path / "elsewhere"
    d.mkdir()
    return d


@pytest.fixture
def project(tmp_path):
    """A private copy of the synthetic consumer; returns its manifest."""
    return copy_fixture("project", tmp_path / "consumer") / "gold-targets.yaml"


@pytest.fixture
def isolation_project(tmp_path):
    return copy_fixture("project-isolation", tmp_path / "isolation") / "gold-targets.yaml"


@pytest.fixture
def trap(tmp_path):
    return make_trap(tmp_path)


@pytest.fixture
def clone(tmp_path):
    """A runnable copy of this clone (see _clikit.runnable_clone)."""
    return runnable_clone(tmp_path / "clone")
