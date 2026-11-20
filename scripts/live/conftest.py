import itertools
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Make the `livetest` package importable from a plain source checkout. The plugin
# itself is loaded via `-p livetest.plugin` in pyproject.toml addopts, NOT via a
# `pytest_plugins` entry here: pytest 8.1+/9 rejects `pytest_plugins` in a
# non-top-level conftest as a hard error, which is exactly what this file becomes
# when the suite is collected from a repo-root `pytest` run.
sys.path.insert(0, str(Path(__file__).resolve().parent))   # make `livetest` importable


# ---------------------------------------------------------------------------------------
# The fixture further down runs per test, which is too late for one thing: the plugin's
# pytest_configure deletes the service-provision registry in its state directory, which it bound
# at import from SLT_STATE_DIR (else this checkout's scripts/live). A unit run in a shell set up
# for a real stack therefore deleted that stack's registry, and a running session's workers then
# re-provisioned services it had already provisioned. A session whose `-m` expression can select
# no `live` item touches no service, so it uses a temporary state directory of its own instead.
# Anything else is a real session and is left alone.
# ---------------------------------------------------------------------------------------

_SERVICE_MARKERS = ("live",)


def _selects_no_service_item(markexpr: str) -> bool:
    """True when no assignment of markers that includes `live` satisfies `markexpr`. Every marker
    name in the expression is tried both ways, so `-m postgres` -- which selects a live case marked
    `live` and `postgres` -- is a real session."""
    if not markexpr.strip():
        return False
    try:
        from _pytest.mark.expression import Expression
        expression = Expression.compile(markexpr)
    except Exception:  # noqa: BLE001 - pytest reports a bad -m itself; treat it as a real session
        return False
    names = (set(re.findall(r"[\w:+\-.\[\]\\/]+", markexpr)) - {"and", "or", "not"}) \
        | set(_SERVICE_MARKERS)
    if len(names) > 12:
        return False
    for forced in _SERVICE_MARKERS:
        others = sorted(names - {forced})
        for values in itertools.product((False, True), repeat=len(others)):
            on = dict(zip(others, values), **{forced: True})
            if expression.evaluate(lambda name, **_kwargs: on.get(name, False)):
                return False
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Before the plugin's own pytest_configure clears the provision registry."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return          # the controller already redirected it, and the worker inherits that
    if not _selects_no_service_item(config.option.markexpr or ""):
        return
    plugin = sys.modules.get("livetest.plugin")     # loaded by `-p livetest.plugin` (addopts)
    names = ("SLT_STATE_DIR", "SLT_FRAMEWORK_HOME", "SLT_STACK_PREFIX")
    saved = {name: os.environ.get(name) for name in names}
    saved_dir = getattr(plugin, "_STATE_DIR", None)
    state = tempfile.mkdtemp(prefix="slt-hermetic-state-")
    os.environ["SLT_STATE_DIR"] = state
    os.environ.pop("SLT_FRAMEWORK_HOME", None)
    os.environ.pop("SLT_STACK_PREFIX", None)
    if plugin is not None:
        plugin._STATE_DIR = Path(state)     # bound at plugin import, before this hook could run

    def restore():      # runs after pytest_unconfigure
        if plugin is not None:
            plugin._STATE_DIR = saved_dir
        shutil.rmtree(state, ignore_errors=True)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    config.add_cleanup(restore)


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
    monkeypatch.setenv("SLT_CASE_RECORDS", "off")   # in-process cases write no records into a checkout
    # The .env layer is environment too: a developer's .env must not reach a hermetic test.
    from livetest import paths
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {})
    # Docker free space likewise: the build gate would otherwise ask the real daemon.
    from livetest import docker_disk
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (None, "hermetic test"))
