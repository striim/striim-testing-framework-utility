"""Depth markers follow the manifest's declared depth and select the intended cases.

Wires the `depth:` key L1 put on every manifest into pytest markers, so a runner can select
a tier with `-m "depth_regression"` the same way it already selects a service with
`-m spanner`. Two layers, mirroring the existing per-service marker tests in
test_plugin_routing.py:

  * `_declared_depth` (collection-time raw-YAML read, tolerant of unparseable/missing depth --
    same rationale as `_declared_services`: collection must not fail on a manifest
    `load_manifest` would reject).
  * `LiveItem.__init__` stamps `depth_<value>` alongside `live` and the service markers.

The `--collect-only -m depth_smoke` count below is the smoke tier of the framework's own
regression tree (hello, framework, services), which rule B alone produces. A test repo's
regression/gate counts pin its own corpus and stay in your test repo.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from livetest.manifest import VALID_DEPTHS
from livetest.plugin import _declared_depth
from tests import _hermetic_child

REPO = pathlib.Path(__file__).resolve().parents[3]
LIVE_ROOT = REPO / "scripts" / "live"


def _child_env() -> dict:
    """The collection child sees this tree, not the host's settings files or project (tests/_hermetic_child.py).
    Module-scoped callers run before scripts/live/conftest.py removes the host's SLT_, so it is removed here."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("SLT_")}
    return _hermetic_child.child_env(LIVE_ROOT / ".no-such-settings-file", base=base)

# The smoke count (rule B) of the framework's own regression tree. The regression/gate
# collection counts pin a consumer's own corpus and stay with it. 43 since the
# gcs, kafka, spanner and vertica cdc-diff cases (see their README.md files).
EXPECTED_SMOKE = 43


# ---- _declared_depth (collection-time raw read, mirrors _declared_services) ----------------

def test_declared_depth_reads_the_key(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\ndepth: fault\nassert:\n  smoke: true\n")
    assert _declared_depth(p) == "fault"

def test_declared_depth_is_none_when_absent(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nassert:\n  smoke: true\n")
    assert _declared_depth(p) is None

def test_declared_depth_tolerates_unparseable_yaml(tmp_path):
    # Same contract as _declared_services: an unreadable manifest gets no depth marker rather
    # than crashing collection. Its error belongs at RUN time (load_manifest), where it names
    # the file and the reason.
    p = tmp_path / "test.yaml"
    p.write_text("name: [unclosed\n")
    assert _declared_depth(p) is None

def test_declared_depth_ignores_non_string_value(tmp_path):
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\ndepth: 7\nassert:\n  smoke: true\n")
    assert _declared_depth(p) is None


# ---- marker registration ---------------------------------------------------------------

def test_all_seven_depth_markers_are_registered_in_pyproject():
    # Keeps VALID_DEPTHS (manifest.py) and pyproject's registration in lockstep -- an
    # unregistered marker warns at use, and the suite may run warnings as errors.
    text = (LIVE_ROOT / "pyproject.toml").read_text()
    assert VALID_DEPTHS == {"smoke", "gate", "regression", "fault", "customer", "measure", "canary"}
    for depth in VALID_DEPTHS:
        assert f'"depth_{depth}:' in text, f"depth_{depth} not registered in pyproject.toml"

def test_pytest_markers_lists_all_seven(_markers_output):
    for depth in VALID_DEPTHS:
        assert f"depth_{depth}" in _markers_output, f"depth_{depth} missing from --markers output"


@pytest.fixture(scope="module")
def _markers_output():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        capture_output=True, text=True, cwd=str(LIVE_ROOT), env=_child_env(),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---- collection counts (the exact numbers L1 established) --------------------------------

def _collect_count(marker: str) -> int:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", marker],
        capture_output=True, text=True, cwd=str(LIVE_ROOT), env=_child_env(),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    last = [ln for ln in r.stdout.splitlines() if ln.strip()][-1]
    if last.startswith("no tests collected"):
        return 0
    # e.g. "43/2643 tests collected (2600 deselected) in 2.57s"
    return int(last.split("/", 1)[0])


def test_collect_only_depth_smoke_finds_exactly_43():
    assert _collect_count("depth_smoke") == EXPECTED_SMOKE

def test_no_test_fails_on_an_unknown_depth_marker():
    # A marker used but not registered is a collection-time warning (or error, under
    # -W error) rather than a per-test failure; assert none fired.
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "depth_smoke",
         "-W", "error::pytest.PytestUnknownMarkWarning"],
        capture_output=True, text=True, cwd=str(LIVE_ROOT), env=_child_env(),
    )
    assert r.returncode == 0, r.stdout + r.stderr
