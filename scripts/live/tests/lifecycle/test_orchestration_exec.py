"""`recover:` and the `stop_start_cycle` / `service_outage` actions, executed through the real
``livetest.plugin`` with the exec harness's fakes (tests/lifecycle/exec_harness.py).

The helpers these phases call (``recovery``, ``outage``, ``node_fault``) have tests of their own;
these tests pin what only the plugin decides: the order of interrupt, restore, post_recover seed
and assertions, that a restore which does not bring the app back fails the case, which fault
target a node fault uses each cycle, and what a concurrent SQL loop's errors mean.

Order is asserted as a subsequence of the fake's event log, never as the whole log, so a status
read added between two steps does not change what these tests prove.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import exec_harness
from .exec_harness import _agree, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "orchestration"
NODE_FAULTS = {"SLT_ENABLE_NODE_FAULTS": "1"}     # the opt-in a node fault needs; the target is fake

# A Striim client that STOPs, STARTs and reports what it was told; a concurrent loop whose runs
# are recorded; a fake outage and a fake node fault target. Every wait below is on an event the
# fake itself sets, so no outcome depends on how fast the host schedules a thread.
EXTRA = r'''
import threading as _threading

FEED_TRIED, FEED_RAN, FEED_FAILED = _threading.Event(), _threading.Event(), _threading.Event()
_real_status = FakeClient.current_status


def _current_status(self, app, timeout=None):
    return STATE.get("status") or _real_status(self, app)


def _stop_app(self, app):
    if SC.get("feed_gate"):                    # the concurrent loop has run before the first STOP
        (FEED_FAILED if SC.get("feed_fail") == "always" else FEED_RAN).wait(30)
    log("stop_app", app=app)
    STATE["running"], STATE["status"] = False, None


def _start_app(self, app):
    log("start_app", app=app)
    if SC.get("restore_fail"):                 # the START is accepted and the app never gets to RUNNING
        STATE["status"] = "STARTING"
        return
    STATE["running"], STATE["status"] = True, None


FakeClient.current_status = _current_status
FakeClient.stop_app = _stop_app
FakeClient.start_app = _start_app

_run_sql = FakePgAdmin.run_sql


def _run_sql_with_feed(self, sql):
    if "slt-feed" not in sql:
        return _run_sql(self, sql)
    FEED_TRIED.set()
    fail = SC.get("feed_fail")                 # "always", "once" or absent
    if STATE.get("down") or fail == "always" or (fail == "once" and not STATE.get("feed_failed")):
        STATE["feed_failed"] = True
        log("feed_error")
        FEED_FAILED.set()
        raise RuntimeError("could not connect to server: Connection refused")
    _run_sql(self, sql)
    log("feed_ok")
    FEED_RAN.set()


FakePgAdmin.run_sql = _run_sql_with_feed

P.outage.container_for = lambda service, env=None: (f"slt-{service}", "")
P.outage.hooks_for = lambda service, signal, env=None, services_dir=None: (None, None)


def _outage_cycle(container, signal, down_for, ready_timeout, wait, **kw):
    if SC.get("feed_fail") != "always":
        FEED_RAN.wait(30)                      # the loop delivered before the outage
    log("service_down", container=container, signal=signal)
    STATE["down"] = True
    FEED_FAILED.wait(30)                       # and failed while the service was down
    STATE["down"] = bool(SC.get("feed_fail") == "always")
    log("service_up", container=container)


def _watch_app(client, app, settle, poll=2.0, report=None):
    log("watch_app", app=app)
    return "RUNNING"


P.outage.cycle = _outage_cycle
P.outage.watch_app = _watch_app

import livetest.node_fault as _node_fault


class _FakeNodeTarget:
    def __init__(self, client, ctx, selection, app=None, nodes=None, refresh=False, record=None, timeout=None):
        log("node_target", selection=selection, refresh=refresh)

    def check_owner(self, left):
        log("node_check_owner")

    def interrupt(self, mode):
        log("node_interrupt", mode=mode)
        STATE["running"] = False

    def restore(self, expect_running=True, settle=0.0):
        log("node_restore", settle=settle)
        STATE["running"] = True
        return "RUNNING"

    def close(self):
        log("node_target_close")


_node_fault.Target = _FakeNodeTarget
FakeClient.configure_failover = lambda self, servers: log("configure_failover", servers=servers)
'''


def _run(run_case, monkeypatch, case, edit=None, env=None, **scenario):
    # The pre_deploy seed reaches the target as an initial load at the first status read after the
    # deploy; the exact read of that target (logged "select") is the assertion phase in the log.
    monkeypatch.setattr(exec_harness, "CONFTEST", exec_harness.CONFTEST + EXTRA)
    return run_case(case, {"initial_load": True, **scenario}, fixtures=FIXTURES, edit=edit, env=env)


def _in_order(names, *wanted):
    """The positions of ``wanted`` as a subsequence of ``names``; fails naming the first one missing."""
    at, out = -1, []
    for w in wanted:
        try:
            at = names.index(w, at + 1)
        except ValueError:
            raise AssertionError(f"{w!r} does not follow {out and names[out[-1]]!r} in {names}") from None
        out.append(at)
    return out


def _inserts(run, value):
    return [i for i, e in enumerate(run.events()) if e["event"] == "insert" and e["ids"] == [value]]


# --- recover: ----------------------------------------------------------------------------

def test_exec_recover_stops_and_restores_each_cycle_then_seeds_post_recover_then_asserts(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "recover-stop", mirror="live")
    _agree(run, "passed")
    names = run.names()
    stop1, start1, stop2, start2, read = _in_order(
        names, "deploy", "stop_app", "start_app", "stop_app", "start_app", "select")[1:]
    assert names.count("stop_app") == names.count("start_app") == 2
    [late] = _inserts(run, 12)                 # the post_recover seed ran once,
    assert start2 < late < read                # after the LAST restore and before the assertions
    assert "teardown_namespace" in names[read:]


def test_exec_recover_restore_that_never_reaches_running_fails_the_case(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "recover-stop", mirror="live", restore_fail=True)
    _agree(run, "failed")
    names = run.names()
    _in_order(names, "stop_app", "start_app")
    assert names.count("start_app") == 1       # no second cycle after a restore that failed
    assert "did not come back after the restore" in run.junit()[2]
    assert not _inserts(run, 12)               # nothing seeded post_recover
    assert "select" not in names         # and nothing asserted against an app that is down
    assert "teardown_namespace" in names


def test_exec_recover_node_fault_uses_a_fresh_target_each_cycle(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "recover-node", env=NODE_FAULTS)
    _agree(run, "passed")
    targets = [e for e in run.events() if e["event"] == "node_target"]
    # one built (with a node refresh) before the first interruption, one more for the second cycle
    assert [(t["selection"], t["refresh"]) for t in targets] == [("primary", True), ("primary", False)]
    names = run.names()
    _in_order(names, "node_target", "node_check_owner", "node_interrupt", "node_restore",
              "node_target", "node_check_owner", "node_interrupt", "node_restore", "select")
    assert "stop_app" not in names and "start_app" not in names     # the target did both, not the app API
    _in_order(names, "select", "node_target_close", "configure_failover")   # closed, failover reset, at the end
    assert [e["servers"] for e in run.events() if e["event"] == "configure_failover"] == [[]]


def test_exec_recover_node_fault_on_another_namespaces_flow_is_refused(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "recover-node", env=NODE_FAULTS, edit=lambda y: y.replace(
        "node: primary, times: 2, after: 0, every: 0,", 'node: {hosting: "Elsewhere.Flow"},'))
    _agree(run, "failed")
    assert "invalid-node-fault: hosting flow must belong to this case" in run.junit()[2]
    names = run.names()
    assert "node_target" not in names and "node_interrupt" not in names
    assert "select" not in names


# --- action: stop_start_cycle ---------------------------------------------------------------

def test_exec_action_stop_start_cycle_restarts_each_cycle_while_the_feed_delivers(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "stop-cycle-feed", feed_gate=True)
    _agree(run, "passed")
    names = run.names()
    _in_order(names, "deploy", "feed_ok", "stop_app", "start_app", "stop_app", "start_app", "select")
    assert names.count("stop_app") == names.count("start_app") == 2
    assert "feed_error" not in names


def test_exec_action_concurrent_feed_error_fails_the_case(run_case, monkeypatch):
    # one failed run among successes: no outage expects it, so the case fails after the cycles
    run = _run(run_case, monkeypatch, "stop-cycle-feed", feed_gate=True, feed_fail="once")
    _agree(run, "failed")
    junit = run.junit()[2]
    assert "action concurrent SQL" in junit
    assert re.search(r"feed\.sql on postgres-source: \d+ run\(s\) succeeded, 1 failed "
                     r"\(first: could not connect to server", junit), junit
    names = run.names()
    assert names.count("start_app") == 2       # both cycles ran before the feed was judged
    assert "select" not in names


def test_exec_action_concurrent_feed_that_never_succeeds_fails_the_case(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "stop-cycle-feed", feed_gate=True, feed_fail="always")
    _agree(run, "failed")
    assert re.search(r"feed\.sql on postgres-source: 0 run\(s\) succeeded, \d+ failed", run.junit()[2])
    assert "feed_ok" not in run.names()


# --- action: service_outage -----------------------------------------------------------------

def test_exec_action_service_outage_tolerates_feed_errors_on_the_downed_service(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, "service-outage")
    _agree(run, "passed")
    names = run.names()
    down, error, up, watch, read = _in_order(
        names, "service_down", "feed_error", "service_up", "watch_app", "select")
    assert names.index("feed_ok") < down       # it delivered before the outage
    assert [e["container"] for e in run.events() if e["event"] == "service_down"] == ["slt-postgres"]


def test_exec_action_service_outage_feed_that_never_succeeds_fails_the_case(run_case, monkeypatch):
    # errors on the downed service's route are expected; a loop that never once succeeded is not
    run = _run(run_case, monkeypatch, "service-outage", feed_fail="always")
    _agree(run, "failed")
    assert re.search(r"feed\.sql on postgres-source: 0 run\(s\) succeeded, \d+ failed", run.junit()[2])
    names = run.names()
    _in_order(names, "service_down", "service_up", "watch_app")
    assert "select" not in names
