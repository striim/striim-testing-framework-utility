"""Bring-up defect 0: ``--parallel`` spawns exactly ONE xdist layer.

On the live-tier host (2026-09-15) ``striim-test run --parallel`` took the host down: xdist hands each worker the
controller's argv, rebuilds its config and calls ``pytest_cmdline_main`` in the worker, so livetest's
hook turned every worker into a controller with 3 workers of its own. The real-xdist test below runs
the livetest hook in a probe plugin that DEFUSES a worker configured as a controller (and records
it), so a regression fails the test instead of fork-bombing the host.

The root conftest puts ``scripts/live`` on the path.
"""
import os
import types

import pytest

pytest_plugins = ["pytester"]

PARALLEL = 3          # striim-test's PARALLEL_WORKERS

_PROBE = '''
import os
import pytest
from livetest.plugin import pytest_addoption  # noqa: F401  (registers --parallel)
from livetest.plugin import pytest_cmdline_main as _livetest_cmdline_main


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    _livetest_cmdline_main(config)
    if hasattr(config, "workerinput") and (config.option.numprocesses or config.option.tx):
        with open(os.environ["BRINGUP_PROBE_LOG"], "a") as f:
            f.write("RESPAWN " + config.workerinput["workerid"] + "\\n")
        config.option.numprocesses = 0
        config.option.dist = "no"
        config.option.tx = []
'''

_CASES = '''
import os
import pytest


@pytest.mark.parametrize("i", range(9))
def test_case(i):
    with open(os.environ["BRINGUP_PROBE_LOG"], "a") as f:
        f.write("TEST %s %s %d\\n" % (os.environ.get("PYTEST_XDIST_WORKER"),
                                      os.environ.get("PYTEST_XDIST_WORKER_COUNT"), os.getpid()))
'''


def _cfg(**extra):
    cfg = types.SimpleNamespace(option=types.SimpleNamespace(parallel=PARALLEL, numprocesses=None, dist="no", tx=[]))
    for k, v in extra.items():
        setattr(cfg, k, v)
    return cfg


def test_hook_leaves_a_worker_config_alone(monkeypatch):
    from livetest.plugin import pytest_cmdline_main
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    cfg = _cfg(workerinput={"workerid": "gw0"})
    pytest_cmdline_main(cfg)
    assert (cfg.option.numprocesses, cfg.option.dist, cfg.option.tx) == (None, "no", [])
    assert "SLT_PARALLEL" not in os.environ


def test_hook_configures_the_controller_with_three_workers(monkeypatch):
    from livetest.plugin import pytest_cmdline_main
    monkeypatch.delenv("SLT_PARALLEL", raising=False)
    cfg = _cfg()
    pytest_cmdline_main(cfg)
    assert cfg.option.numprocesses == PARALLEL and cfg.option.tx == ["popen"] * PARALLEL
    assert os.environ.get("SLT_PARALLEL") == "1"


def test_real_xdist_workers_never_respawn_workers(pytester, monkeypatch):
    live = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "live")
    log = pytester.path / "probe.log"
    pytester.makepyfile(bringup_probe=_PROBE, test_cases=_CASES)
    for var in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID",
                "PYTEST_ADDOPTS", "SLT_PARALLEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BRINGUP_PROBE_LOG", str(log))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(pytester.path), os.path.abspath(live)]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    result = pytester.runpytest_subprocess("-p", "xdist.plugin", "-p", "bringup_probe", "-p", "no:cacheprovider",
                                           "--parallel", str(PARALLEL), "test_cases.py", timeout=180)

    lines = log.read_text().splitlines() if log.exists() else []
    assert [ln for ln in lines if ln.startswith("RESPAWN")] == [], "an xdist worker configured itself as a controller"
    tests = [ln.split() for ln in lines if ln.startswith("TEST")]
    assert result.ret == 0, result.stdout.str()
    assert len(tests) == 9
    assert {t[1] for t in tests} <= {f"gw{i}" for i in range(PARALLEL)}       # every case ran in a first-layer worker
    assert {t[2] for t in tests} == {str(PARALLEL)}
    assert len({t[3] for t in tests}) <= PARALLEL
