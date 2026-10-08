import os
import sys
from pathlib import Path

import pytest

# Make the `livetest` package importable from a plain source checkout. The plugin
# itself is loaded via `-p livetest.plugin` in pyproject.toml addopts, NOT via a
# `pytest_plugins` entry here: pytest 8.1+/9 rejects `pytest_plugins` in a
# non-top-level conftest as a hard error, which is exactly what this file becomes
# when the suite is collected from a repo-root `pytest` run.
sys.path.insert(0, str(Path(__file__).resolve().parent))   # make `livetest` importable


# ---------------------------------------------------------------------------------------
# Hermetic means hermetic: the stack env must not reach a test that isn't marked `live`.
#
# Every parallel-stack variable (SLT_STACK_PREFIX, the SLT_*_HOST_PORT set, STRIIM_URL) is
# read by the code under test -- that is the whole point of them -- so a developer shell
# running a second stack silently rewrote the expected values underneath the unit suite.
# On 2026-08-19 that was 20 failures out of 1344 in `pytest -m "not live"`: container names
# came back `alt-slt-oracle` instead of `slt-oracle`, and every opregistry filename picked
# up the STRIIM_URL cluster tag. None of them were real; all of them looked real, and they
# only appear on the machines that most need the suite to be trustworthy.
#
# A test that genuinely wants one of these sets it itself (monkeypatch.setenv runs after
# this fixture, so it still wins). A test that wants the AMBIENT stack is by definition
# talking to it, and carries the `live` marker.
# ---------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic_stack_env(request, monkeypatch):
    """Scrub the parallel-stack env for every test not marked `live`."""
    if request.node.get_closest_marker("live"):
        return
    for name in [n for n in os.environ if n.startswith("SLT_")] + ["STRIIM_URL"]:
        monkeypatch.delenv(name, raising=False)
    # The .env layer is environment too: a developer's .env must not reach a hermetic test.
    from livetest import paths
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {})
    # Docker free space likewise: the build gate would otherwise ask the real daemon.
    from livetest import docker_disk
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (None, "hermetic test"))
