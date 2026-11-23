"""Hermetic integration suite: no service's pre_up hook ever runs from here (SLT_PRE_UP=0 for
this process and its children; inttest.services.run_pre_up checks it)."""
import os

import pytest

os.environ["SLT_PRE_UP"] = "0"


@pytest.fixture(autouse=True)
def _private_lock_dir(monkeypatch, tmp_path):
    """The session and compose locks are machine-wide (livetest.stack's lock dir). Each test gets a lock dir of
    its own, in this process and for the pytest sessions it starts, as each already gets its own state dir:
    otherwise a child session meets the lock this session holds and is refused as a second session."""
    from livetest import stack
    monkeypatch.setattr(stack, "_LOCK_DIR", tmp_path / "slt-locks")
    monkeypatch.setenv("SLT_LOCK_DIR", str(tmp_path / "slt-locks"))


@pytest.fixture(autouse=True)
def _no_release_matrix(monkeypatch):
    """A developer's matrix or STRIIM_HOME must not change jar names; tests that need a release set
    pass one explicitly."""
    from inttest import opjarrelease
    monkeypatch.setattr(opjarrelease, "configured_versions", lambda *a, **k: set())


@pytest.fixture(autouse=True)
def _no_pre_up(monkeypatch):
    monkeypatch.setenv("SLT_PRE_UP", "0")
    monkeypatch.setenv("SLT_CASE_RECORDS", "off")   # in-process cases write no records into a checkout
    # The tier reads the project manifest's servicesRoots (inttest.resources.consumer_roots):
    # a developer's GOLD_TARGETS must not add consumer services to a hermetic test.
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
    # A host's SLT_PARALLEL=1 (field lanes and gold-cli set it) put the child sessions of the teardown
    # tests into parallel mode and failed them; a test that needs it sets it itself.
    monkeypatch.delenv("SLT_PARALLEL", raising=False)


@pytest.fixture(autouse=True)
def _no_bind_gateway_probe(monkeypatch):
    """A harness `compose up` asks Docker for the bridge gateway (livetest.bindaddr). Docker is a stub
    here, so the answer is fixed at Docker Desktop's (none); probe tests call detect_gateway."""
    from livetest import bindaddr
    monkeypatch.setattr(bindaddr, "_cached_gateway", lambda run=None: None)
