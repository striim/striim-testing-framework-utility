"""`stop_start_cycle` proves each STOP took before it STARTs the app again, executed through the
real ``livetest.plugin`` with the exec harness's fakes (tests/lifecycle/exec_harness.py).

A STOP is asynchronous and `striim_api` does not raise on a rejected one, so without a status read
between STOP and START a case whose STOP never happened runs undisturbed and passes."""
from __future__ import annotations

from pathlib import Path

from . import exec_harness
from .exec_harness import _agree, run_case  # noqa: F401 - run_case is a fixture

pytest_plugins = ["pytester"]

FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "appactions"

# The STOP leaves whatever `stop_leaves` names for that stop (1-based), CREATED by default; a
# STOP that leaves RUNNING is one the server rejected. The verification budget is cut to 1s so
# a STOP that never takes fails in seconds.
EXTRA = r'''
def _stop_app(self, app):
    STATE["stops"] = STATE.get("stops", 0) + 1
    leaves = SC.get("stop_leaves", {}).get(str(STATE["stops"]), "CREATED")
    log("stop_app", app=app, leaves=leaves)
    STATE["status"] = leaves
    if leaves != "RUNNING":
        STATE["running"] = False


def _start_app(self, app):
    log("start_app", app=app)
    STATE["status"] = None
    STATE["running"] = True


_real_status = FakeClient.current_status


def _current_status(self, app, timeout=None):
    status = STATE.get("status") or _real_status(self, app)
    log("status", status=status)
    return status


FakeClient.stop_app = _stop_app
FakeClient.start_app = _start_app
FakeClient.current_status = _current_status
P.recovery.verify_timeout = lambda manifest_timeout: 1.0
'''


def _run(run_case, monkeypatch, **scenario):
    monkeypatch.setattr(exec_harness, "CONFTEST", exec_harness.CONFTEST + EXTRA)
    return run_case("stop-cycle", scenario, fixtures=FIXTURES)


def _cycle_records(env):
    return [r for r in env["assertions"]
            if r["type"] == "recovery" and r["spec"].get("action") == "stop_start_cycle"]


def test_each_cycle_starts_the_app_only_after_the_stop_settled(run_case, monkeypatch):
    run = _run(run_case, monkeypatch)
    env = _agree(run, "passed")
    ev = run.events()
    stops = [i for i, e in enumerate(ev) if e["event"] == "stop_app"]
    starts = [i for i, e in enumerate(ev) if e["event"] == "start_app"]
    assert len(stops) == len(starts) == 2
    for stop, start in zip(stops, starts):
        # a status read between them saw the app stopped
        assert any(e["event"] == "status" and e["status"] == "CREATED" for e in ev[stop:start])
    records = _cycle_records(env)
    assert [r["spec"]["cycle"] for r in records] == [1, 2]
    assert all(r["status"] == "passed" for r in records)
    assert [r["actual"]["rows"][0]["status"] for r in records] == ["CREATED", "CREATED"]


def test_a_stop_that_never_takes_fails_the_case_as_not_interrupted(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, stop_leaves={"1": "RUNNING"})
    env = _agree(run, "failed")
    assert "not interrupted" in run.junit()[2]
    assert "start_app" not in run.names()            # no START after a STOP that did not happen
    [record] = _cycle_records(env)
    assert record["status"] == "failed" and record["actual"]["rows"][0]["status"] == "RUNNING"


def test_a_stop_that_crashes_the_app_fails_the_case(run_case, monkeypatch):
    run = _run(run_case, monkeypatch, stop_leaves={"2": "CRASH"})
    env = _agree(run, "failed")
    assert "CRASH" in run.junit()[2]
    assert run.names().count("start_app") == 1       # cycle 1 restarted; the crashed cycle 2 is not
    records = _cycle_records(env)
    assert [r["status"] for r in records] == ["passed", "failed"]
    assert records[1]["actual"]["rows"][0]["status"] == "CRASH"
