import functools
import itertools
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Make the `inttest` package importable from a plain source checkout. The plugin
# itself is loaded via `-p inttest.plugin` in pyproject.toml addopts.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# pytest's built-in `pytester` plugin is opt-in -- declaring it here (rather than
# passing `-p pytester` to every invocation) makes the `pytester` fixture available
# to any test in this tree, for tests that need to run a real, isolated pytest
# session against inttest.plugin's own hooks (collection/selection/UsageError),
# not just call its functions directly.
pytest_plugins = ["pytester"]


# ---------------------------------------------------------------------------------------
# The fixture further down runs per test, which is too late for one thing: the plugin takes the
# session lock in pytest_configure, in the state directory SLT_STATE_DIR (else
# SLT_FRAMEWORK_HOME) names. A unit run in a shell set up for a real stack therefore took that
# stack's lock -- refused while a real session ran, blocking one while it ran itself, and
# leaving lock files in that checkout. A session whose `-m` expression can select no
# `integration` or `docker` item touches no service, so it coordinates in a temporary
# directory of its own instead. Anything else is a real session and is left alone.
# ---------------------------------------------------------------------------------------

_SERVICE_MARKERS = ("integration", "docker")


def _selects_no_service_item(markexpr: str) -> bool:
    """True when no assignment of markers that includes `integration` (or `docker`) satisfies
    `markexpr`. Every marker name in the expression is tried both ways, so `-m postgres` -- which
    selects a regression case marked `integration` and `postgres` -- is a real session."""
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
    """Before the plugin's own pytest_configure takes the session lock."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return          # the controller already redirected it, and the worker inherits that
    if not _selects_no_service_item(config.option.markexpr or ""):
        return
    names = ("SLT_STATE_DIR", "SLT_FRAMEWORK_HOME", "INT_STACK_PREFIX", "SLT_LOCK_DIR")
    saved = {name: os.environ.get(name) for name in names}
    state = tempfile.mkdtemp(prefix="int-hermetic-state-")
    os.environ["SLT_STATE_DIR"] = state
    os.environ.pop("SLT_FRAMEWORK_HOME", None)
    os.environ.pop("INT_STACK_PREFIX", None)
    # The session and compose locks are machine-wide (livetest.stack's lock dir, read at its import),
    # so they move into the temporary directory too.
    from livetest import stack
    saved_lock_dir = stack._LOCK_DIR
    os.environ["SLT_LOCK_DIR"] = os.path.join(state, "locks")
    stack._LOCK_DIR = Path(os.environ["SLT_LOCK_DIR"])

    def restore():      # runs after pytest_unconfigure, so the lock is already released
        stack._LOCK_DIR = saved_lock_dir
        shutil.rmtree(state, ignore_errors=True)
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    config.add_cleanup(restore)


# ---------------------------------------------------------------------------------------
# Hermetic means hermetic: the stack env must not reach a test that isn't marked
# `integration`. Twin of scripts/live/conftest.py's fixture of the same name -- same
# failure, found the same day.
#
# Every parallel-stack variable is read by the code under test, so a developer shell
# running a second stack silently rewrote the expected values underneath the unit suite:
# `declared_containers` returned `alt-int-postgres` where the test spelled `int-postgres`,
# and `build_tokens` returned the alt stack's ports where the test spelled the defaults.
#
# Two families are scrubbed, because two resolvers read two vocabularies:
#   - INT_*        -- compose's published ports (docker_env) and the per-key client
#                     overrides (live_env), read by inttest/tokens.py;
#   - the `provides:` TOKEN names themselves (POSTGRES_PORT, ORACLE_HOST, ...) -- read
#     by inttest/plugin.py's fixture-path resolvers as bare os.getenv overrides.
# Deriving the second list from services/*/service.yaml rather than hardcoding it means a
# new service's tokens are covered the day it lands.
#
# A test that genuinely wants one sets it itself (monkeypatch.setenv runs after this
# fixture, so it still wins). A test that wants the AMBIENT stack is talking to real
# Docker and carries the `integration` marker.
# ---------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _published_token_names() -> frozenset:
    """Every `${TOKEN}` name services/*/service.yaml publishes -- which is exactly the set
    of bare `os.getenv` overrides plugin.py's fixture-path resolvers honor.

    Cached: this parses four YAML files, and an autouse fixture calls it once per test.
    Safe to cache because service.yaml is checkout state, not environment state -- the
    env half of the scrub list is recomputed live in `_stack_env_names` below."""
    from inttest import paths
    from inttest import tokens as tokens_mod

    names = set()
    services_dir = paths.services_dir()
    if not services_dir.is_dir():
        return frozenset()
    for service in sorted(p.name for p in services_dir.iterdir() if p.is_dir()):
        try:
            raw = tokens_mod._load_service_def(service, services_dir)
        except tokens_mod.ServiceConfigError:
            continue  # a services/ subdir that is not a service (no service.yaml)
        names.update(raw.get("provides") or {})
    return frozenset(names)


def _stack_env_names() -> list:
    """Every env var that can redirect this tier at a different stack."""
    names = {n for n in os.environ if n.startswith("INT_")}
    # Read by plugin.py's _spanner_tokens but not published by services/spanner's provides:.
    names.add("SPANNER_ADMIN_PORT")
    # Sends Google's Spanner client to an emulator; inttest/spanneradmin.py refuses it in real mode.
    names.add("SPANNER_EMULATOR_HOST")
    # The Bigtable client's emulator redirect; plugin.py sets it for a case and refuses a different one.
    names.add("BIGTABLE_EMULATOR_HOST")
    # shared makes services.shared_reuse inspect real containers before compose up.
    names.add("SLT_INFRA_OWNERSHIP")
    names |= _published_token_names()
    return sorted(names)


@pytest.fixture(autouse=True)
def _hermetic_stack_env(request, monkeypatch):
    """Scrub the parallel-stack env for every test not marked `integration`."""
    if request.node.get_closest_marker("integration"):
        return
    for name in _stack_env_names():
        monkeypatch.delenv(name, raising=False)
    # The .env layer is environment too: a developer's .env must not reach a hermetic test.
    from inttest import paths
    monkeypatch.setattr(paths, "dotenv_values", lambda env=None: {})
