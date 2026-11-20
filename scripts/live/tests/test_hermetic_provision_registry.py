"""The hermetic live suite keeps out of the state directory the shell names.

The plugin's pytest_configure deletes the service-provision registry in its state directory, bound at
plugin import from SLT_STATE_DIR (else the checkout's scripts/live), before any per-test env scrub
runs. A unit run in a shell set up for a real stack therefore deleted that stack's registry, and a
running session's workers then re-provisioned services it had already provisioned. A session whose
marker expression can select no `live` item now uses a temporary state directory of its own.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
REGISTRY = ".slt-provision-registry.json"


def _env(state: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLT_", "INT_", "PYTEST_", "COVERAGE_", "STRIIM_", "GOLD_"))}
    env.update(SLT_STATE_DIR=str(state), SLT_LOCK_DIR=str(state.parent / "locks"), **extra)
    return env


def _pytest(env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args,
                           "tests/test_isolation.py"], cwd=HERE, env=env, capture_output=True, text=True,
                          timeout=300)


def _state(tmp_path, prefix=""):
    state = tmp_path / "state"
    state.mkdir()
    reg = state / ((f".{prefix}-" + REGISTRY[1:]) if prefix else REGISTRY)
    reg.write_text('{"slt-postgres": "provisioned by a running session"}')
    return reg


@pytest.mark.parametrize("markexpr", ["not live", "not live and not postgres", "postgres and not live"])
def test_the_hermetic_suite_leaves_the_named_registry_alone(tmp_path, markexpr):
    reg = _state(tmp_path)
    r = _pytest(_env(reg.parent), "-m", markexpr)
    assert r.returncode in (0, 5), r.stdout[-800:] + r.stderr[-800:]
    assert reg.exists(), "a unit run deleted a real session's provision registry"


def test_a_prefixed_stacks_registry_is_left_alone_too(tmp_path):
    reg = _state(tmp_path, prefix="alt")
    r = _pytest(_env(reg.parent, SLT_STACK_PREFIX="alt"), "-m", "not live")
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert reg.exists()


def test_the_hermetic_suite_under_xdist_leaves_it_alone(tmp_path):
    reg = _state(tmp_path)
    r = _pytest(_env(reg.parent, SLT_PARALLEL="1"), "-m", "not live", "-n", "2")
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert reg.exists()


@pytest.mark.parametrize("markexpr", ["live", "postgres", "not postgres"])
def test_a_session_that_can_select_live_items_still_clears_its_registry(tmp_path, markexpr):
    """Only the marker expression decides: `-m postgres` selects live cases (marked `live` and
    `postgres`), so it is a real session, and a real session starts from an empty registry."""
    reg = _state(tmp_path)
    r = _pytest(_env(reg.parent), "-m", markexpr)
    assert r.returncode in (0, 5), r.stdout[-800:] + r.stderr[-800:]
    assert not reg.exists()
