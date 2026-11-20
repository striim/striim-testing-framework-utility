"""The hermetic suite keeps out of the state directory the shell names.

The session lock is taken in pytest_configure, before any per-test env scrub runs, so a unit run
in a shell that named a real stack's state directory took that stack's lock: refused while a real
session ran, and blocking one while it ran itself. A session whose marker expression can select no
`integration` or `docker` item now coordinates in a temporary directory of its own.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import FileLock

HERE = Path(__file__).resolve().parents[1]
HERMETIC = "not integration and not docker"


def _env(state: Path | None, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLT_", "INT_", "PYTEST_", "COVERAGE_"))}
    if state is not None:
        env["SLT_STATE_DIR"] = str(state)
    env.update(extra)
    return env


def _pytest(env: dict, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           *args, "tests/test_waevent.py"], cwd=HERE, env=env,
                          capture_output=True, text=True, timeout=300)


def test_a_held_real_session_lock_does_not_refuse_the_hermetic_suite(tmp_path):
    state, locks = tmp_path / "state", tmp_path / "locks"
    state.mkdir(), locks.mkdir()
    # A developer's second stack, as a field shell names it; its session lock is machine-wide. timeout=0: a
    # fresh file nothing else holds, and no FileLock here may wait (test_no_filelock_carries_a_wait).
    with FileLock(str(locks / ".alt-int-session.lock"), timeout=0):
        r = _pytest(_env(state, INT_STACK_PREFIX="alt", SLT_LOCK_DIR=str(locks)), "-m", HERMETIC)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert "another integration-test session" not in r.stdout + r.stderr


def test_the_hermetic_suite_leaves_nothing_in_another_checkout(tmp_path):
    """The review's shell: no SLT_STATE_DIR, and SLT_FRAMEWORK_HOME naming a second checkout, whose
    scripts/integration is then the state directory."""
    home = tmp_path / "other-checkout"
    other = home / "scripts" / "integration"
    other.mkdir(parents=True)
    # A checkout of this framework made of links, except the directory the state would land in.
    for src, dst in ((HERE.parents[1], home), (HERE.parent, home / "scripts"), (HERE, other)):
        for child in src.iterdir():
            if not (dst / child.name).exists():
                (dst / child.name).symlink_to(child)
    before = sorted(p.name for p in other.iterdir())
    r = _pytest(_env(None, SLT_FRAMEWORK_HOME=str(home)), "-m", HERMETIC)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert sorted(p.name for p in other.iterdir()) == before


@pytest.mark.parametrize("markexpr", ["integration", "postgres", "not docker"])
def test_a_session_that_can_select_integration_items_still_takes_the_real_lock(tmp_path, markexpr):
    """Only the marker expression decides: `-m postgres` selects a regression case (marked
    `integration` and `postgres`), so it is a real session and the lock stays where it was."""
    state, locks = tmp_path / "state", tmp_path / "locks"
    state.mkdir(), locks.mkdir()
    with FileLock(str(locks / ".int-session.lock"), timeout=0):
        r = _pytest(_env(state, SLT_LOCK_DIR=str(locks)), "-m", markexpr)
    assert r.returncode == 4, r.stdout[-800:] + r.stderr[-800:]
    assert "another integration-test session is already running" in r.stdout + r.stderr
