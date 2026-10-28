"""The integration and perf tiers must not exit 0 when they proved nothing (live's
``test_infra_guard.py``, for this tier).

A certification run's integration step skipped all seven of a module's cases on a host without
the service, exited 0, and its row read green. The guard fires only when at least one case
skipped because this host could not run it (``EnvSkip``) and no case executed.
"""
import types

import pytest

from inttest import plugin
from inttest.plugin import EnvSkip, _env_skip, _int_all_skipped_guard


class _Rep:
    def __init__(self, nodeid, reason=None, case=True, cause=""):
        self.nodeid = nodeid
        self.longrepr = ("f.py", 1, f"Skipped: {reason}") if reason else None
        if case:
            self._int_case = True
        self._int_env_skip = cause


class _Session:
    def __init__(self, stats):
        tr = types.SimpleNamespace(stats=stats)
        self.config = types.SimpleNamespace(
            pluginmanager=types.SimpleNamespace(get_plugin=lambda name: tr))


A = "regression/op/x/test.yaml::x"
B = "regression/op/y/test.yaml::y"


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    monkeypatch.delenv("INT_ALLOW_ALL_SKIPPED", raising=False)


def test_every_case_skipped_by_the_host_fails_the_run():
    s = _Session({"skipped": [_Rep(A, "docker not available", cause="env"),
                              _Rep(B, "docker not available", cause="env")]})
    assert _int_all_skipped_guard(s, 0) == 1


def test_a_partial_run_keeps_its_exit_status():
    s = _Session({"skipped": [_Rep(A, "service unavailable", cause="env")], "passed": [_Rep(B)]})
    assert _int_all_skipped_guard(s, 0) == 0


def test_declared_skips_do_not_fire_it():
    s = _Session({"skipped": [_Rep(A, "disabled (known bug)"), _Rep(B, "set INT_X=1")]})
    assert _int_all_skipped_guard(s, 0) == 0


def test_passing_unit_tests_do_not_count_as_a_case_executing():
    s = _Session({"skipped": [_Rep(A, "STRIIM_HOME is not set", cause="env")],
                  "passed": [_Rep("tests/test_tokens.py::test_tid", case=False)]})
    assert _int_all_skipped_guard(s, 0) == 1


def test_the_opt_out_holds_for_absent_infrastructure(monkeypatch):
    monkeypatch.setenv("INT_ALLOW_ALL_SKIPPED", "1")
    s = _Session({"skipped": [_Rep(A, "docker not available", cause="env")]})
    assert _int_all_skipped_guard(s, 0) == 0


def test_the_opt_out_does_not_cover_a_build_that_could_not_run(monkeypatch):
    monkeypatch.setenv("INT_ALLOW_ALL_SKIPPED", "1")
    s = _Session({"skipped": [_Rep(A, "cannot build operator jar", cause="build")]})
    assert _int_all_skipped_guard(s, 0) == 1


def test_a_failing_run_keeps_its_own_status():
    s = _Session({"skipped": [_Rep(A, "docker not available", cause="env")]})
    assert _int_all_skipped_guard(s, 2) == 2


def test_an_env_skip_is_a_pytest_skip_carrying_its_cause():
    with pytest.raises(pytest.skip.Exception) as e:
        _env_skip("build", "cannot build the inttest harness jar")
    assert isinstance(e.value, EnvSkip) and e.value.cause == "build"
    assert e.value.msg == "cannot build the inttest harness jar"


def test_the_host_skip_sites_raise_env_skips(monkeypatch):
    # STRIIM_HOME unset: the module build is a host prerequisite, not a declared skip.
    monkeypatch.delenv("STRIIM_HOME", raising=False)
    with pytest.raises(EnvSkip) as e:
        plugin._build_operator_jar("case", "op/X")
    assert e.value.cause == "env"
