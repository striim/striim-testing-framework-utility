from types import SimpleNamespace

import pytest

from livetest import nothing, lifecycle
from livetest.assertions import AssertionFailed


class Clock:
    def __init__(self): self.t = 0
    def wall(self): return str(self.t)
    def monotonic(self): return self.t
    def sleep(self, seconds):
        assert seconds >= 0
        self.t += seconds


def spec():
    return {"target": "${PG_TARGET_SCHEMA}.${TID}tgt", "db": "postgres-target",
            "nothing": {"window": "2s", "where": {"id": 1}}}


def test_missing_sentinel_refuses_before_read():
    with pytest.raises(AssertionFailed, match="nothing-sentinel-unproven"):
        nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"}, completion=None,
                               read=lambda *a: pytest.fail("must not read"), clock=Clock())


def test_full_window_is_required():
    clock = Clock()
    reads = []
    result = nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
        completion={"kind": "sentinel", "reason": "satisfied"}, clock=clock,
        read=lambda *a: reads.append(clock.t) or [{"id": 999}])
    assert result[0]["status"] == "passed" and clock.t >= 2
    assert reads[0] == 0 and reads[-1] >= 2


def test_transport_keeps_a_real_budget_at_the_boundary():
    budgets = []
    nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
        completion={"kind": "sentinel", "reason": "satisfied"}, clock=Clock(),
        read=lambda spec, remaining: budgets.append(remaining) or [])
    assert min(budgets) >= 1


def test_late_arrival_fails():
    clock = Clock()
    with pytest.raises(AssertionFailed, match="nothing-arrived"):
        nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
            completion={"kind": "sentinel", "reason": "satisfied"}, clock=clock,
            read=lambda *a: [{"id": 1}] if clock.t >= 1 else [])


def test_slow_initial_snapshot_cannot_replace_boundary_read():
    clock = Clock()
    def read(*args):
        if clock.t == 0:
            clock.t = 2.1
            return []
        return [{"id": 1}]
    with pytest.raises(AssertionFailed, match="nothing-arrived"):
        nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
            completion={"kind": "sentinel", "reason": "satisfied"}, clock=clock, read=read)


@pytest.mark.parametrize("row", [{"id": "1"}, {}, {"id": None}])
def test_unclassifiable_output_cannot_pass_absence(row):
    with pytest.raises(AssertionFailed, match="nothing-read-error"):
        nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
            completion={"kind": "sentinel", "reason": "satisfied"}, clock=Clock(), read=lambda *a: [row])


def test_nothing_needs_same_route_and_completion_sentinel():
    raw = {"assert": {"data": [spec()]}}
    with pytest.raises(ValueError, match="invalid-nothing-spec"):
        nothing.validate(raw, "p", None)
    lc = SimpleNamespace(completion={"kind": "sentinel"}, sentinel={"observe": {
        "db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}other", "key": "id"}})
    with pytest.raises(ValueError, match="invalid-nothing-spec"):
        nothing.validate(raw, "p", lc)


@pytest.mark.parametrize("sink,observe", [
    ("file", {"path": "${OWNED_DIR}/rows.json", "key": "data.id", "deletion": {"metadata.OperationName": "DELETE"}}),
    ("kafka", {"db": "kafka", "topic": "${KAFKA_TGT_TOPIC}", "key": "value.id", "deletion": {"value.op": "DELETE"}}),
    ("db", {"db": "spanner-google", "table": "${TID}tgt", "key": "id"}),
])
def test_new_sentinel_sinks_load(sink, observe):
    block = {"version": 1, "mode": "cdc", "sink": sink,
        "readiness": {"kind": "sentinel"}, "completion": {"kind": "sentinel"},
        "sentinel": {"db": "postgres-source", "insert": "i.sql", "delete": "d.sql", "observe": observe}}
    assert lifecycle.parse_spec(block, "p").sentinel["observe"] == observe


@pytest.mark.parametrize("sink", ["file", "kafka", "spanner"])
@pytest.mark.parametrize("delivered", [True, False])
def test_new_sink_presence_precedes_delete(monkeypatch, sink, delivered):
    from livetest import sinkprobe
    ref = ({"path": "${OWNED_DIR}/rows.json", "key": "id", "deletion": {"op": "DELETE"}}
           if sink == "file" else {"db": "kafka", "topic": "${KAFKA_TGT_TOPIC}", "key": "id", "deletion": {"op": "DELETE"}}
           if sink == "kafka" else {"db": "spanner-google", "table": "${TID}tgt", "key": "id"})
    s = lifecycle.parse_spec({"version": 1, "mode": "cdc", "sink": "db" if sink == "spanner" else sink,
        "readiness": {"kind": "sentinel"}, "completion": {"kind": "sentinel"}, "stability": 0,
        "sentinel": {"db": "postgres-source", "insert": "insert.sql", "delete": "delete.sql", "observe": ref}}, "p")
    values, actions = [], []
    def write(sql, remaining):
        actions.append(sql)
        if delivered:
            if sql == "insert": values.append({"id": 42, "op": "INSERT"})
            elif sink == "spanner": values.clear()
            else: values.append({"id": 42, "op": "DELETE"})
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: SimpleNamespace(run=write))
    monkeypatch.setattr(sinkprobe, "rows", lambda *a: list(values))
    sentinel = lifecycle.Sentinel(s, {"postgres-source": {"admin": object()}}, {},
        SimpleNamespace(per_test="case", attempt="attempt"), "done", {},
        lambda name: name.split(".")[0], id_source=lambda: 42)
    watch = lifecycle.Watch("sentinel", "delivery", lifecycle.Deadline(2, Clock()))
    assert sentinel.run(watch) == ("satisfied" if delivered else "deadline")
    assert actions == (["insert", "delete"] if delivered else ["insert"])
