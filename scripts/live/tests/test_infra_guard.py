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
# The three assignments in LiveItem._runtest are the only places the flag is ever set, and no
# hermetic test can execute runtest (it needs live services). Without these assertions, deleting
# the cluster-unreachable assignment -- the one line that fixes the bug this feature exists for
# -- leaves every other test in this file green while the silent pass returns in full. Pinned at
# source level, the idiom test_plugin_routing.py already uses for this method.

def test_runtest_marks_exactly_the_three_blocked_paths():
    import inspect
    src = inspect.getsource(plugin.LiveItem._runtest)
    assert src.count("self._slt_infra_skip =") == 3, (
        "three sites mean 'we could not test this': cluster unreachable, Docker absent, the OP "
        "jar could not be built. A fourth needs a deliberate decision, not a silent addition."
    )


def test_each_cause_is_bound_to_the_skip_it_guards():
    """Counting the literals is not enough: swapping two of them passes a count-and-membership
    check while inverting the behaviour -- the cluster outage would stop offering the opt-out
    and the broken build would start offering it. So each cause is pinned to the SPECIFIC skip
    that follows it, which also catches a mark whose skip is conditional or moved away.
    """
    import inspect
    lines = inspect.getsource(plugin.LiveItem._runtest).splitlines()
    expected = {
        '"cluster"': "_slt_striim_reason",
        '"docker"': "unavailable (no Docker)",
        '"build"': "{m.name}: {e}",
    }
    seen = set()
    for i, line in enumerate(lines):
        if "self._slt_infra_skip =" not in line:
            continue
        cause = next((c for c in expected if c in line), None)
        assert cause, "unknown cause on line %d: %s" % (i, line.strip())
        seen.add(cause)
        window = " ".join(lines[i + 1:i + 3])
        assert "pytest.skip(" in window, (
            "line %d marks the item but does not skip immediately: %s" % (i, line.strip()))
        assert expected[cause] in window, (
            "%s is not on the skip it is supposed to describe; got: %s" % (cause, window.strip()))
    assert seen == set(expected)


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
