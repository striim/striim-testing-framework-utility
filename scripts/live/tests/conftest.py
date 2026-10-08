"""Hermetic live suite: no service's pre_up hook ever runs from here.

A pre_up hook (service.yaml, livetest.prestart) may fetch gigabytes (a VM's disks). Tests drive
bring-ups with stubbed compose, so hooks are switched off for this process and anything it starts
(SLT_PRE_UP=0), and prestart.run_hook is stubbed per test. The tests of the hook itself take
`real_prestart_run`.
"""
import os

import pytest

os.environ["SLT_PRE_UP"] = "0"

from livetest import prestart as _prestart  # noqa: E402
from livetest.ggtrail import harness as _ggtrail  # noqa: E402

REAL_RUN = _prestart.run_hook

# The ggtrail harness lives under the project root (SLT_PROJECT_ROOT). The tests decide to skip
# on it at import; the hermetic fixture then scrubs SLT_* before each test runs, so the harness
# would resolve back to this clone. Pinned to the import-time directory, both agree.
_GGTRAIL_DIR = _ggtrail._harness_dir()


@pytest.fixture(autouse=True)
def _ggtrail_harness_dir(monkeypatch):
    monkeypatch.setattr(_ggtrail, "_harness_dir", lambda: _GGTRAIL_DIR)


@pytest.fixture(autouse=True)
def _op_jar_store(monkeypatch, tmp_path):
    """Content-named OP jar copies go to the test's tmp dir, never the engine's state dir."""
    from livetest import opartifacts
    monkeypatch.setattr(opartifacts, "op_jar_store", lambda: tmp_path / ".slt-op-jars")


@pytest.fixture(autouse=True)
def _no_pre_up(monkeypatch):
    from livetest import prestart
    monkeypatch.setenv("SLT_PRE_UP", "0")
    monkeypatch.setattr(prestart, "run_hook", lambda *a, **k: False)


@pytest.fixture
def real_prestart_run():
    """The unstubbed livetest.prestart.run_hook."""
    return REAL_RUN
