"""Root tests: both engines on the path, and no ambient location or stack settings.

The engine suites under scripts/live and scripts/integration carry their own pytest config.
These tests span both tiers, so this puts ``scripts/live`` and ``scripts/integration`` on
``sys.path`` (no install needed) and scrubs what a developer shell or ``.env`` could leak in.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ["SLT_PRE_UP"] = "0"

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "scripts" / "integration", _REPO / "scripts" / "live"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for name in [n for n in os.environ if n.startswith(("SLT_", "INT_"))] + ["STRIIM_URL"]:
        monkeypatch.delenv(name, raising=False)
    # No project manifest carried over from an earlier test (doctor and the CLI activate one in
    # process): example:/jar: roots resolve through the active project at call time.
    from livetest import layout as _layout, project as _project
    monkeypatch.setattr(_project, "_ACTIVE", None)
    _layout.set_manifest_roots(services=(), state=None)
    # Never a service's pre_up hook (it may fetch gigabytes), here or in a child.
    monkeypatch.setenv("SLT_PRE_UP", "0")
    from inttest import paths as int_paths
    from livetest import paths as live_paths
    for mod in (live_paths, int_paths):
        monkeypatch.setattr(mod, "dotenv_values", lambda env=None: {})
    # Docker free space: doctor and the build gate would otherwise ask the real daemon.
    from livetest import docker_disk
    monkeypatch.setattr(docker_disk, "vm_free_bytes", lambda run=None: (None, "hermetic test"))
