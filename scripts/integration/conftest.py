import functools
import os
import sys
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
