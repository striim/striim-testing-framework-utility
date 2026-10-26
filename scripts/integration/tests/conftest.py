"""Hermetic integration suite: no service's pre_up hook ever runs from here (SLT_PRE_UP=0 for
this process and its children; inttest.services.run_pre_up checks it)."""
import os

import pytest

os.environ["SLT_PRE_UP"] = "0"


@pytest.fixture(autouse=True)
def _no_release_matrix(monkeypatch, tmp_path):
    """A developer's STRIIM_BUILD_* matrix (shell or project .env) must not change jar names."""
    for key in [k for k in os.environ if k.startswith("STRIIM_BUILD_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("FTC_PUBLISHER_RELEASES_ENV", str(tmp_path / "no-release-matrix.env"))


@pytest.fixture(autouse=True)
def _no_pre_up(monkeypatch):
    monkeypatch.setenv("SLT_PRE_UP", "0")
    # The tier reads the project manifest's servicesRoots (inttest.resources.consumer_roots):
    # a developer's GOLD_TARGETS must not add consumer services to a hermetic test.
    monkeypatch.delenv("GOLD_TARGETS", raising=False)
