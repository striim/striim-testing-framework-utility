"""Depth markers follow the manifest's declared depth and select the intended cases.

Wires the `depth:` key L1 put on every manifest into pytest markers, so a runner can select
a tier with `-m "depth_regression"` the same way it already selects a service with
`-m spanner`. Two layers, mirroring the existing per-service marker tests in
test_plugin_routing.py:

  * `_declared_depth` (collection-time raw-YAML read; a missing depth is `regression`, the
    loader's default). Collection also runs `load_manifest`, so a manifest it rejects (a
    misspelt depth, an unknown key, unparseable YAML) is a collection error in every tier.
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

def test_declared_depth_defaults_to_regression_when_absent(tmp_path):
    # load_manifest defaults a missing depth to regression, so the marker must agree: a case
    # without `depth:` runs under `-m depth_regression` instead of being dropped by it.
    p = tmp_path / "test.yaml"
    p.write_text("name: x\ntql: app.tql\nassert:\n  smoke: true\n")
    assert _declared_depth(p) == "regression"

def test_an_unparseable_manifest_is_a_collection_error(tmp_path):
    # _declared_depth still never raises on one; the loader, which collection now runs, names
    # the file and the YAML error instead of leaving it to run time (or to no time at all, when
    # a -m filter deselected the unmarked case).
    rc, out = _collect_case(tmp_path, "", "depth_smoke", raw="name: [unclosed\n")
    assert rc not in (0, 5), out[-800:]
    assert "invalid YAML" in out and "dcase" in out, out[-800:]


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



# ---- a case's depth selects it, and a manifest error is a collection error -------------------

_CASE = "name: {name}\npurpose: a depth case\ntql: app.tql\n{extra}assert:\n  smoke: true\n"


def _collect_case(tmp_path, extra, marker, name="dcase", raw=None):
    case = tmp_path / "cases" / name
    case.mkdir(parents=True)
    (case / "test.yaml").write_text(raw if raw is not None else _CASE.format(name=name, extra=extra))
    (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "INT_", "PYTEST_", "COVERAGE_", "STRIIM"))}
    env.update(SLT_LOCK_DIR=str(tmp_path / "locks"), SLT_CASE_RECORDS="off", SLT_PRE_UP="0")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "livetest.plugin", "-p", "no:cacheprovider",
                        "--import-mode=importlib", "--collect-only", "-q", "-m", marker, str(tmp_path / "cases")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout + r.stderr


def test_a_declared_depth_selects_its_tier(tmp_path):
    rc, out = _collect_case(tmp_path, "depth: smoke\n", "depth_smoke")
    assert rc == 0 and "1 test collected" in out, out[-800:]


def test_a_case_is_not_selected_by_another_tier(tmp_path):
    rc, out = _collect_case(tmp_path, "depth: smoke\n", "depth_gate")
    assert rc == 5 and "1 deselected" in out, out[-800:]


def test_a_case_without_depth_is_selected_as_regression(tmp_path):
    rc, out = _collect_case(tmp_path, "", "depth_regression")
    assert rc == 0 and "1 test collected" in out, out[-800:]


def test_a_misspelt_depth_is_a_collection_error_in_every_tier(tmp_path):
    # Before: `depth: smok` got no marker, `-m depth_smoke` deselected it, and the loader that
    # would refuse it never ran -- exit 5 and nothing naming the typo.
    rc, out = _collect_case(tmp_path, "depth: smok\n", "depth_smoke")
    assert rc not in (0, 5), out[-800:]
    assert "error" in out.lower() and "'depth' must be one of" in out and "dcase" in out, out[-800:]


def test_any_manifest_error_is_a_collection_error_even_when_its_tier_is_filtered_out(tmp_path):
    rc, out = _collect_case(tmp_path, "depth: smoke\ntimout: 30\n", "depth_gate")
    assert rc not in (0, 5), out[-800:]
    assert "unknown manifest key(s) ['timout']" in out, out[-800:]


def test_every_depth_marker_is_registered_by_the_plugin_for_strict_markers(tmp_path):
    # A project's own configuration need not list the depth markers: under --strict-markers an
    # unregistered depth_regression (every manifest without depth:) was a collection error.
    for name, extra in (("nodepth", ""), ("smokecase", "depth: smoke\n"), ("canarycase", "depth: canary\n")):
        case = tmp_path / "cases" / name
        case.mkdir(parents=True)
        (case / "test.yaml").write_text(_CASE.format(name=name, extra=extra))
        (case / "app.tql").write_text("CREATE APPLICATION ${APP};\nEND APPLICATION ${APP};\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    live: live case\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLT_", "INT_", "PYTEST_", "COVERAGE_", "STRIIM"))}
    env.update(SLT_LOCK_DIR=str(tmp_path / "locks"), SLT_CASE_RECORDS="off", SLT_PRE_UP="0")
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "livetest.plugin", "-p", "no:cacheprovider",
                        "--import-mode=importlib", "--collect-only", "-q", "--strict-markers",
                        "-W", "error::pytest.PytestUnknownMarkWarning", str(tmp_path / "cases")],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode == 0 and "3 tests collected" in out, out[-800:]
