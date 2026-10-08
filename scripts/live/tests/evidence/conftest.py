"""Evidence tests: hermetic defaults. No ambient run identity, stack or ownership env leaks in.

``run_case`` is the executed-plugin harness from tests/lifecycle/exec_harness.py (loaded by path: the two
test directories are separate rootdir-relative packages)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRUB = ("SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES", "SLT_STACK_PREFIX", "SLT_RUN_EPOCH", "SLT_INVOCATION_ID",
          "SLT_RUN_IDENTITY", "SLT_PARALLEL", "SLT_STATE_DIR", "STRIIM_URL", "PYTEST_XDIST_WORKER")


def _harness():
    name = "_slt_exec_harness"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "lifecycle" / "exec_harness.py")
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


run_case = _harness().run_case


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for name in _SCRUB:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLT_INVOCATION_ID", "0a0b0c0d0e0f40a18b2c3d4e5f607182")   # every run has one (C5 1.9.0)
    yield
