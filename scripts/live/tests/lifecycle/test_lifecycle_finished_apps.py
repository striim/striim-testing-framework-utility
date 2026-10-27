"""The RUNNING gate of a lifecycle case and an app that has already finished.

A bounded source (DatabaseReader initial load, QuiesceOnILCompletion) can load its rows and
reach COMPLETED -- QUIESCED on some releases -- before the gate's first read. For an initial-load
case that app has run: the gate is satisfied and the readiness and completion counts decide. For
a cdc case a finished app ends the gate at once. Fake clock, fake probes and fake clients only.
"""
from __future__ import annotations

import pytest

from livetest import lifecycle
from livetest.lifecycle import LifecycleError

from tests.lifecycle.test_lifecycle_deadlines import (FakeClock, _admins, _fakes, _initial, _kw,  # noqa: F401
                                                      _owned, _spec, TOKENS)


def _run_initial_load(status, source=3, target=3, apps=None):
    clock, state = FakeClock(), lifecycle.State("initial-load")
    spec = _initial()
    admins = _admins(source_fn=lambda s, p: [(source,)], target_fn=lambda s, p: [(target,)])
    lifecycle.record_baseline(state, spec, admins, TOKENS, clock=clock, owned=_owned)
    kw = _kw(clock, admins, statuses=status if callable(status) else (lambda a: status))
    if apps:
        kw["apps"] = apps
    return clock, state, spec, kw


@pytest.mark.parametrize("finished", ["COMPLETED", "QUIESCED"])
def test_initial_load_app_finished_before_running_satisfies_the_gate(finished):
    clock, state, spec, kw = _run_initial_load(finished)
    smoke = lifecycle.smoke_and_ready(state, spec, **kw)
    assert state.running["reason"] == "satisfied"
    assert state.running["witness"] == f"observed NS.lcApp {finished} (bounded source finished)"
    assert smoke[0]["status"] == "passed"
    assert smoke[0]["detail"] == f"NS.lcApp {finished} (bounded source finished) (bounded)"
    assert "RUNNING" not in smoke[0]["detail"] and "RUNNING" not in state.ready["condition"]
    assert state.ready["reason"] == "satisfied"
    # H4 on the same finished app: the counts decide, the status is not re-gated
    lifecycle.complete(state, spec, **kw)
    assert state.completion["reason"] == "satisfied"
    assert clock.t - 1000.0 < 5.0                                   # no wait for the deadline


def test_initial_load_mixed_running_and_finished_apps_names_each():
    clock, state, spec, kw = _run_initial_load(lambda a: "COMPLETED" if a == "NS.il" else "RUNNING",
                                               apps=["NS.il", "NS.other"])
    smoke = lifecycle.smoke_and_ready(state, spec, **kw)
    assert state.running["witness"] == "observed NS.il COMPLETED (bounded source finished), NS.other RUNNING"
    assert smoke[0]["detail"] == "NS.il COMPLETED (bounded source finished), NS.other RUNNING (bounded)"


def test_initial_load_completed_app_with_missing_rows_still_fails_on_the_data():
    clock, state, spec, kw = _run_initial_load("COMPLETED", source=3, target=2)
    with pytest.raises(LifecycleError, match="readiness baseline-landed failed: deadline") as ei:
        lifecycle.smoke_and_ready(state, spec, **kw)
    assert state.running["reason"] == "satisfied" and state.ready["reason"] == "deadline"
    assert {o["value"] for o in state.ready["observations"]} == {2}
    assert ei.value.records[0]["status"] == "passed"           # the app ran; the data did not land
    # and completion, on its own, fails on the short target too
    state2 = lifecycle.State("initial-load")
    state2.baseline = dict(state.baseline)
    with pytest.raises(LifecycleError, match="completion source-count failed: deadline"):
        lifecycle.complete(state2, spec, **_kw(FakeClock(), kw["admins"], statuses=lambda a: "COMPLETED"))


@pytest.mark.parametrize("finished", ["COMPLETED", "QUIESCED"])
def test_cdc_app_finished_before_running_fails_fast(finished):
    clock, state = FakeClock(), lifecycle.State("cdc")
    spec = _spec(deadlines={"readiness": 600, "completion": 5})
    with pytest.raises(LifecycleError, match=f"terminal-status:{finished} \\(NS.lcApp\\) before RUNNING") as ei:
        lifecycle.smoke_and_ready(state, spec, **_kw(clock, _admins(source_fn=lambda s, p: [(True,)]),
                                                     statuses=lambda a: finished))
    assert state.running["reason"] == f"terminal-status:{finished} (NS.lcApp)" and state.running["witness"] is None
    assert ei.value.records[0]["type"] == "smoke" and ei.value.records[0]["status"] == "failed"
    assert clock.t - 1000.0 < 60.0                                  # long before the 600 s deadline


def test_initial_load_halt_is_still_terminal():
    clock, state, spec, kw = _run_initial_load("HALT")
    with pytest.raises(LifecycleError, match="terminal-status:HALT \\(NS.lcApp\\) before RUNNING"):
        lifecycle.smoke_and_ready(state, spec, **kw)
    assert state.running["reason"] == "terminal-status:HALT (NS.lcApp)"
