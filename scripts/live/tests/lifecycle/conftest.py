"""Hermetic defaults for the lifecycle tests: no ambient stack or ownership env, and a
private machine-wide lock dir per test."""
import pytest

_SCRUB = ("SLT_INFRA_OWNERSHIP", "SLT_KEEP_SERVICES", "SLT_STACK_PREFIX", "SLT_RUN_EPOCH",
          "SLT_PARALLEL", "SLT_OPS_PRELOADED", "SLT_SERVICES_HOST", "SLT_STATE_DIR", "STRIIM_URL",
          "PYTEST_XDIST_WORKER")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    for name in _SCRUB:
        monkeypatch.delenv(name, raising=False)
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setenv("SLT_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("SLT_INVOCATION_ID", "0a0b0c0d0e0f40a18b2c3d4e5f607182")   # every run has one (C5 1.9.0)
    yield
