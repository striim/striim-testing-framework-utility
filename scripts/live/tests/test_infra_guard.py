"""The live tier must not exit 0 when it proved nothing.

**The failure this closes was observed, not imagined.** With the Striim cluster down, every
live item skipped, pytest reported ``39 skipped`` and exited **0**, and that was read as green
twice in one session before anyone noticed. An all-skipped pass retroactively makes every green
claim ever sourced from this tier unfalsifiable, because nobody can tell afterwards which runs
executed anything.

The guard fires only when BOTH hold — at least one LIVE item was skipped for an infrastructure
reason, and no live item executed. Everything else keeps its exit status.
"""
import types

import pytest

from livetest import plugin
from livetest.plugin import _slt_infra_guard


class _Rep:
    """A pytest report double.

    Both facts the guard reads are ATTRIBUTES stamped in ``pytest_runtest_makereport``, not text
    inside the skip reason — so this double carries them the same way a real report does, and a
    reason string stays a reason string.
    """

    def __init__(self, nodeid, reason=None, live=True, infra=""):
        self.nodeid = nodeid
        self.longrepr = ("f.py", 1, f"Skipped: {reason}") if reason else None
        if live:
            self._slt_live = True
        self._slt_infra_skip = infra


class _Session:
    def __init__(self, stats):
        tr = types.SimpleNamespace(stats=stats)
        self.config = types.SimpleNamespace(
            pluginmanager=types.SimpleNamespace(get_plugin=lambda name: tr))


LIVE = "regression/op/x/test.yaml::x"
LIVE2 = "regression/op/y/test.yaml::y"


def _unit(nodeid="tests/test_stack.py::test_prefix", reason=None):
    return _Rep(nodeid, reason, live=False)


def _blocked(nodeid=LIVE, reason="no reachable Striim", cause="cluster"):
    return _Rep(nodeid, reason, infra=cause)


# --------------------------------------------------------------------------- the decision

def test_all_live_skipped_on_infrastructure_fails_the_run(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked(), _blocked(LIVE2)]})
    assert _slt_infra_guard(s, 0) == 1


def test_a_partial_run_keeps_its_exit_status(monkeypatch):
    # One case ran, another's service was missing. An ordinary skip -- and failing it would
    # train people to ignore the guard, which is how a guard stops working.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked()], "passed": [_Rep(LIVE2)]})
    assert _slt_infra_guard(s, 0) == 0


def test_ordinary_skips_do_not_fire_it(monkeypatch):
    # `disabled:`, an opt-in service gate, a topology mismatch. None is marked infra, so a
    # suite of quarantined cases still exits 0.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_Rep(LIVE, "disabled: known bug"),
                              _Rep(LIVE2, "set SLT_KAFKA=1")]})
    assert _slt_infra_guard(s, 0) == 0


def test_hermetic_passes_do_not_mask_it(monkeypatch):
    # The case that decides how "executed" is counted. A bare `pytest` run puts ~1500 passing
    # UNIT tests in the same stats dict; counting them as execution would let a dead cluster
    # hide behind them.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked()], "passed": [_unit() for _ in range(1500)]})
    assert _slt_infra_guard(s, 0) == 1


def test_a_non_live_infra_skip_does_not_fire_it(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_Rep("tests/t.py::t", "x", live=False, infra="cluster")]})
    assert _slt_infra_guard(s, 0) == 0


def test_a_failed_live_case_counts_as_executed(monkeypatch):
    # exitstatus=0 beside a failed report is not a state real pytest produces; it is chosen so
    # the assertion measures the GUARD's decision rather than pytest's.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked()], "failed": [_Rep(LIVE2)]})
    assert _slt_infra_guard(s, 0) == 0


def test_an_errored_live_case_counts_as_executed(monkeypatch):
    # An item that dies in setup or teardown lands in stats["error"], not "failed".
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked()], "error": [_Rep(LIVE2)]})
    assert _slt_infra_guard(s, 0) == 0


@pytest.mark.parametrize("bucket", ["xpassed", "xfailed"])
@pytest.mark.parametrize("cause", ["cluster", "docker"])
def test_an_expected_failure_outcome_counts_as_executed(monkeypatch, bucket, cause):
    # An xfail'd case deployed and ran its assertions; pytest files it under "xpassed" or
    # "xfailed", not "passed"/"failed". Beside an unrelated infra skip that is a partial run.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked(cause=cause)], bucket: [_Rep(LIVE2)]})
    assert _slt_infra_guard(s, 0) == 0


@pytest.mark.parametrize("bucket", ["xpassed", "xfailed"])
def test_a_non_live_expected_failure_does_not_mask_it(monkeypatch, bucket):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked()], bucket: [_unit()]})
    assert _slt_infra_guard(s, 0) == 1


def test_the_opt_out(monkeypatch):
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "1")
    assert _slt_infra_guard(_Session({"skipped": [_blocked()]}), 0) == 0


def test_an_existing_failure_is_not_overwritten(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    assert _slt_infra_guard(_Session({"skipped": [_blocked()]}), 2) == 2


def test_no_terminalreporter_is_survivable(monkeypatch):
    # No reporter at all (-p no:terminal, an embedding host). NOT the xdist-worker case: xdist
    # never unregisters terminalreporter, so the guard runs on workers too -- harmlessly, since
    # a worker's status is taken from the hook argument and its stdout is /dev/null'd.
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = types.SimpleNamespace(config=types.SimpleNamespace(
        pluginmanager=types.SimpleNamespace(get_plugin=lambda name: None)))
    assert _slt_infra_guard(s, 0) == 0


# --------------------------------------------------------------------------- the two seams
# Everything above tests the DECISION. These test the wiring that lets it reach production at
# all -- both are single lines, and both were deletable with every other test still green.

def test_makereport_stamps_both_flags(monkeypatch):
    """Without these stamps the guard sees no live reports and never fires."""
    class _FakeLive:
        config = None
        _slt_infra_skip = "cluster"

    monkeypatch.setattr(plugin, "LiveItem", _FakeLive)
    monkeypatch.setattr(plugin, "_slt_collect_report", lambda *a, **k: None)
    rep = types.SimpleNamespace()

    gen = plugin.pytest_runtest_makereport(_FakeLive(), None)
    next(gen)
    try:
        gen.send(types.SimpleNamespace(get_result=lambda: rep))
    except StopIteration:
        pass

    assert rep._slt_live is True
    assert rep._slt_infra_skip == "cluster"


def test_an_unmarked_item_is_not_reported_as_infra(monkeypatch):
    # The discriminating half: only the three sites in runtest that mean "we could not test
    # this" set the flag, so an ordinary live case never looks infra-blocked.
    class _FakeLive:
        config = None

    monkeypatch.setattr(plugin, "LiveItem", _FakeLive)
    monkeypatch.setattr(plugin, "_slt_collect_report", lambda *a, **k: None)
    rep = types.SimpleNamespace()
    gen = plugin.pytest_runtest_makereport(_FakeLive(), None)
    next(gen)
    try:
        gen.send(types.SimpleNamespace(get_result=lambda: rep))
    except StopIteration:
        pass
    assert rep._slt_infra_skip == ""


def test_sessionfinish_actually_assigns_the_exit_status(monkeypatch):
    """The single line the whole feature depends on: the guard can compute the right answer
    and throw it away, and nothing else would notice."""
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    monkeypatch.setattr(plugin, "_slt_write_sidecar", lambda cfg: None)
    monkeypatch.setattr(plugin, "_skip_shared_teardown", lambda env: True)

    session = _Session({"skipped": [_blocked()]})
    session.exitstatus = 0
    plugin.pytest_sessionfinish(session, 0)

    assert session.exitstatus == 1


# --------------------------------------------------------------- the marking sites themselves
# The assignments in LiveItem._runtest are executed through the real plugin: cluster unreachable,
# Docker absent and a missing build prerequisite in tests/lifecycle/test_plugin_exec.py
# (test_exec_no_cluster_*, test_exec_docker_absent_*, test_exec_missing_build_prerequisite_*), and
# the two service-unavailable sites (prerequisites, driver jar) in tests/lifecycle/test_outcome_exec.py
# (test_a_service_that_cannot_be_prepared_*, test_a_missing_service_jar_*): each run must exit
# with the banner naming its own cause, so a dropped or swapped mark fails there.


def test_a_build_failure_does_not_offer_the_no_cluster_escape(monkeypatch, capsys):
    """A broken `mvn package` is not an infrastructure fault.

    It still means the run proved nothing, so it still fails -- but printing "set
    SLT_ALLOW_NO_CLUSTER=1" would hand a developer with a broken build a documented way to turn
    it green again, and blaming the cluster sends them to a healthy one.
    """
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    rep = _Rep(LIVE, "LookupOp: mvn package failed in java/OpenProcessors/...")
    rep._slt_infra_skip = "build"
    assert _slt_infra_guard(_Session({"skipped": [rep]}), 0) == 1
    out = capsys.readouterr().out
    assert "SLT_ALLOW_NO_CLUSTER" not in out
    assert "infrastructure" not in out
    assert "mvn package failed" in out          # the real reason still shows


def test_a_cluster_failure_does_offer_it(monkeypatch, capsys):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    assert _slt_infra_guard(_Session({"skipped": [_blocked()]}), 0) == 1
    assert "SLT_ALLOW_NO_CLUSTER" in capsys.readouterr().out


def test_the_opt_out_does_not_cover_a_build_failure(monkeypatch, capsys):
    """`SLT_ALLOW_NO_CLUSTER=1` is about ABSENT INFRASTRUCTURE, and a compile error is not that.

    The trap this closes is a sequence, not a single run: the banner tells a developer to export
    the flag during a cluster outage, and it then sits in their shell. When their `mvn package`
    breaks a week later, every live case skips and -- with the flag checked before the cause was
    known -- the run went green. Removing the flag from the banner did not close it; the gate
    had to move.
    """
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "1")
    rep = _Rep(LIVE, "LookupOp: mvn package failed")
    rep._slt_infra_skip = "build"
    assert _slt_infra_guard(_Session({"skipped": [rep]}), 0) == 1
    assert "SLT_ALLOW_NO_CLUSTER" not in capsys.readouterr().out


def test_the_opt_out_still_covers_a_mixed_run_containing_a_build_failure(monkeypatch):
    # Docker missing AND a module that would not build: the opt-out is refused, because one of
    # the two reasons is not something you can declare absent.
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "1")
    build = _Rep(LIVE2, "Op: mvn package failed"); build._slt_infra_skip = "build"
    s = _Session({"skipped": [_blocked(cause="docker"), build]})
    assert _slt_infra_guard(s, 0) == 1


def test_a_docker_only_run_does_not_blame_the_cluster(monkeypatch, capsys):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked(reason="service postgres unavailable", cause="docker")]})
    assert _slt_infra_guard(s, 0) == 1
    out = capsys.readouterr().out
    assert "Docker was unavailable" in out
    assert "without a cluster" not in out          # the cluster was fine


# ------------------------------------------- a module that was not built, a service that is absent
# A build skip is not absent infrastructure: the module under test (or its prerequisite) is broken,
# the cases that load it were never tested, and the cases that did run cannot vouch for them. So
# it fails the run whatever else ran. A service that is unavailable here is absent infrastructure,
# like Docker: it carries a cause, and the all-skipped rule applies to it.

def test_a_build_skip_fails_the_run_even_when_other_cases_ran(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    build = [_blocked(f"regression/op/m{i}/test.yaml::m{i}", "Op: cannot run mvn", cause="build")
             for i in range(3)]
    s = _Session({"passed": [_Rep(LIVE)], "skipped": build})
    assert _slt_infra_guard(s, 0) == 1


def test_a_build_skip_beside_a_failed_case_keeps_the_failure(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"failed": [_Rep(LIVE)], "skipped": [_blocked(LIVE2, "Op: x", cause="build")]})
    assert _slt_infra_guard(s, 1) == 1


def test_a_partial_run_with_a_build_skip_names_the_case_and_does_not_say_nothing_ran(monkeypatch, capsys):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"passed": [_Rep(LIVE)],
                  "skipped": [_blocked(LIVE2, "YOp: cannot run mvn to build YOp", cause="build")]})
    assert _slt_infra_guard(s, 0) == 1
    out = capsys.readouterr().out
    assert LIVE2 in out and "cannot run mvn to build YOp" in out and "could not be built" in out
    assert "none executed" not in out and "SLT_ALLOW_NO_CLUSTER" not in out


def test_the_opt_out_does_not_cover_a_build_skip_in_a_partial_run(monkeypatch):
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "1")
    s = _Session({"passed": [_Rep(LIVE)], "skipped": [_blocked(LIVE2, "Op: x", cause="build")]})
    assert _slt_infra_guard(s, 0) == 1


def test_all_live_skipped_for_an_unavailable_service_fails_the_run(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked(reason="service teradata unavailable: no driver jar", cause="service"),
                              _blocked(LIVE2, reason="service teradata unavailable: no driver jar",
                                       cause="service")]})
    assert _slt_infra_guard(s, 0) == 1


def test_a_partial_run_with_an_unavailable_service_keeps_its_exit_status(monkeypatch):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"passed": [_Rep(LIVE)],
                  "skipped": [_blocked(LIVE2, "service teradata unavailable: x", cause="service")]})
    assert _slt_infra_guard(s, 0) == 0


def test_the_opt_out_covers_an_unavailable_service(monkeypatch):
    monkeypatch.setenv("SLT_ALLOW_NO_CLUSTER", "1")
    s = _Session({"skipped": [_blocked(reason="service teradata unavailable: x", cause="service")]})
    assert _slt_infra_guard(s, 0) == 0


def test_a_service_only_run_blames_the_service_not_docker_or_the_cluster(monkeypatch, capsys):
    monkeypatch.delenv("SLT_ALLOW_NO_CLUSTER", raising=False)
    s = _Session({"skipped": [_blocked(reason="service teradata unavailable: no driver jar", cause="service")]})
    assert _slt_infra_guard(s, 0) == 1
    out = capsys.readouterr().out
    assert "service teradata unavailable: no driver jar" in out
    assert "A service this run requires was unavailable" in out and "SLT_ALLOW_NO_CLUSTER=1" in out
    assert "Docker was unavailable" not in out and "without a cluster" not in out
