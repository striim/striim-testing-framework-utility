"""Each call of a block-less case's best-effort teardown is bounded, and so is the whole teardown.

``teardown_unowned`` runs after the ownership ledger's cleanup, outside its deadline. Its calls -- an
engine's ``drop_test_tables``, a Kafka topic or GCS bucket delete, the op.upload delete, a FileWriter
output clear -- had no bound of their own, so one hung driver or admin call hung the case's teardown
and the run with it. Each call now gets the ledger's per-operation bound (``CLEANUP_OP_S``), the
whole teardown its deadline (``CLEANUP_DEADLINE_S``); a call that outlives either is a failure that
names the bound, and the calls after it still run while the deadline lasts.
"""
from __future__ import annotations

import inspect
import threading
import time
from types import SimpleNamespace

import pytest

from livetest import ownership
from livetest.plugin import teardown_unowned


@pytest.fixture
def gate():
    released = threading.Event()
    yield released
    released.set()                      # let every abandoned call finish


def _hang(gate):
    def call(*_a):
        gate.wait(30)
    return call


def _admin(**methods):
    return SimpleNamespace(**methods)


def test_a_hung_engine_drop_fails_at_its_bound_and_the_other_deletes_still_run(gate):
    deleted = []
    admins = {"oracle-source": {"admin": _admin(drop_test_tables=_hang(gate))}}
    kafka = _admin(delete_topic=deleted.append)
    gcs = _admin(delete_bucket=deleted.append)
    started = time.monotonic()
    out = teardown_unowned(admins, [("oracle-source", "o.sql")], {"TID_ORACLE": "T1_"},
                           [(gcs, "b1")], [(kafka, "t1")], None, lambda p: None, ["u1"], lambda n: None,
                           op_s=0.3, deadline_s=2)
    assert time.monotonic() - started < 5
    assert "did not finish within 0.3s" in out["failed"]["engine-tables oracle-source"]
    assert deleted == ["b1", "t1"]
    assert set(out["failed"]) == {"engine-tables oracle-source"}
    assert out["running"] == ["engine-tables oracle-source"]       # waited for until the deadline


@pytest.mark.parametrize("kind", ["bucket", "topic", "upload", "file-output"])
def test_each_unowned_delete_is_bounded(gate, kind):
    hang = _hang(gate)
    args = dict(admins={}, ddl_files=[], tokens={}, gcs_cleanup=[], kafka_cleanup=[], file_spec=None,
                clear_files=lambda p: None, upload_names=[], delete_uploads=lambda n: None)
    if kind == "bucket":
        args["gcs_cleanup"] = [(_admin(delete_bucket=hang), "b1")]
        subject = "bucket b1"
    elif kind == "topic":
        args["kafka_cleanup"] = [(_admin(delete_topic=hang), "t1")]
        subject = "topic t1"
    elif kind == "upload":
        args.update(upload_names=["u1"], delete_uploads=hang)
        subject = "upload u1"
    else:
        args.update(file_spec=[{"path": "/out/f", "min_events": 1}], clear_files=hang)
        subject = "file-output /out/f*"
    started = time.monotonic()
    out = teardown_unowned(**args, op_s=0.3, deadline_s=1.5)
    assert time.monotonic() - started < 5
    assert "did not finish within 0.3s" in out["failed"][subject]
    assert out["running"] == [subject]


def test_the_whole_teardown_stops_at_its_deadline(gate):
    hang = _hang(gate)
    started = time.monotonic()
    out = teardown_unowned({}, [], {}, [], [(_admin(delete_topic=hang), f"t{i}") for i in range(10)], None,
                           lambda p: None, [], lambda n: None, op_s=0.3, deadline_s=0.8)
    assert time.monotonic() - started < 3
    assert len(out["attempted"]) == 10                       # every subject is accounted for
    assert set(out["failed"]) == {f"topic t{i}" for i in range(10)}
    not_attempted = [s for s, e in out["failed"].items() if e == "cleanup-deadline: not attempted"]
    assert 5 <= len(not_attempted) <= 8


def test_a_call_that_raises_is_still_recorded_with_its_error():
    def boom(_t):
        raise RuntimeError("no such topic")
    out = teardown_unowned({}, [], {}, [], [(_admin(delete_topic=boom), "t1")], None, lambda p: None,
                           [], lambda n: None)
    assert out["failed"] == {"topic t1": "RuntimeError('no such topic')"}


def test_the_bounds_are_the_ledgers():
    params = inspect.signature(teardown_unowned).parameters
    assert params["op_s"].default == ownership.CLEANUP_OP_S == 60.0
    assert params["deadline_s"].default == ownership.CLEANUP_DEADLINE_S == 600.0


# --- an abandoned delete never runs on into the next case (review item [4]) --------------------------
# A call abandoned at its bound was left running in its daemon thread while the case ended and the next
# one started, and a second delete on the same admin (a GCS bucket or Kafka topic pair) ran on that
# admin's client from two threads at once. (A live case's drop is by its own ${TID} prefix, a hash of
# run, worker and case, so it cannot drop another case's tables; it still held its connection and locks
# into the next case.) The teardown now
# waits for an abandoned call while its deadline lasts, before the case ends and before the next call on
# the same admin starts; one still running at the deadline is reported as running, and the run stops.


def _release_after(gate, seconds):
    t = threading.Timer(seconds, gate.set)
    t.daemon = True
    t.start()


def test_the_teardown_does_not_return_while_an_abandoned_drop_still_runs(gate):
    state = {"running": False}

    def drop(_prefix):
        state["running"] = True
        gate.wait(30)
        time.sleep(0.05)
        state["running"] = False
    _release_after(gate, 0.8)
    out = teardown_unowned({"oracle-source": {"admin": _admin(drop_test_tables=drop)}},
                           [("oracle-source", "o.sql")], {"TID_ORACLE": ""}, [], [], None, lambda p: None,
                           [], lambda n: None, op_s=0.2, deadline_s=20)
    assert state["running"] is False                         # the drop ended before the case did
    assert "did not finish within 0.2s" in out["failed"]["engine-tables oracle-source"]
    assert out["running"] == []


def test_a_second_delete_on_an_admin_waits_for_its_abandoned_first(gate):
    active, peak, deleted = [0], [0], []
    lock = threading.Lock()

    def delete_topic(topic):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            if topic == "t1":
                gate.wait(30)
            deleted.append(topic)
        finally:
            with lock:
                active[0] -= 1
    kafka = _admin(delete_topic=delete_topic)
    _release_after(gate, 0.8)
    out = teardown_unowned({}, [], {}, [], [(kafka, "t1"), (kafka, "t2")], None, lambda p: None, [],
                           lambda n: None, op_s=0.2, deadline_s=20)
    assert peak[0] == 1                                      # never two calls on one admin at once
    assert deleted == ["t1", "t2"]
    assert set(out["failed"]) == {"topic t1"}                # t1 outlived its bound; t2 ran once t1 ended


def test_a_delete_still_running_at_the_deadline_is_reported_as_running(gate):
    kafka = _admin(delete_topic=_hang(gate))
    other = []
    started = time.monotonic()
    out = teardown_unowned({}, [], {}, [], [(kafka, "t1"), (kafka, "t2"), (_admin(delete_topic=other.append), "t3")],
                           None, lambda p: None, [], lambda n: None, op_s=0.2, deadline_s=1.0)
    assert time.monotonic() - started < 3
    assert out["running"] == ["topic t1"]
    assert "still running at the teardown deadline" in out["failed"]["topic t1"]
    assert out["failed"]["topic t2"] == ("not attempted: an earlier delete on the same admin "
                                         "was still running at the teardown deadline")
    assert other == ["t3"]                                   # another admin's delete still ran


def test_a_case_whose_delete_is_still_running_stops_the_run():
    session = SimpleNamespace(shouldfail=False)
    from livetest.plugin import stop_if_teardown_running
    stop_if_teardown_running(session, "case-a", {"running": []})
    assert session.shouldfail is False
    stop_if_teardown_running(session, "case-a", {"running": ["engine-tables oracle-source"]})
    assert "case-a" in session.shouldfail and "engine-tables oracle-source" in session.shouldfail
