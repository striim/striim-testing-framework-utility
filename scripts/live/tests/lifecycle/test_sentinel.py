"""The sentinel state machine (C7.2). Each phase inserts a fresh id, requires
it to be observed PRESENT, only then deletes it, and requires it to be observed ABSENT. An empty target,
a prior attempt's row and a sibling run's row never satisfy it."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from livetest import lifecycle, runident
from livetest.lifecycle import LifecycleError, parse_spec

TOKENS = {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "t123456789_"}
SQL = {"i.sql": "INSERT INTO ${TID}src (id) VALUES (${SENTINEL_ID})",
       "d.sql": "DELETE FROM ${TID}src WHERE id = ${SENTINEL_ID}"}
_INSERT = re.compile(r"INSERT INTO \S+ \(id\) VALUES \((\d+)\)")
_DELETE = re.compile(r"DELETE FROM \S+ WHERE id = (\d+)")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 0.001)

    def wall(self):
        return f"t={self.t:.3f}"


class Pipeline:
    """A source writer and a target reader; ``mirror`` decides whether source changes reach the target."""

    def __init__(self, target=(), mirror=True, writer_error=None):
        self.target = set(target)
        self.mirror = mirror
        self.log = []
        self.writer_error = writer_error

    def writer(self):
        def run(sql, remaining=10.0):
            if self.writer_error:
                raise self.writer_error
            if m := _INSERT.search(sql):
                self.log.append(("insert", int(m.group(1))))
                if self.mirror:
                    self.target.add(int(m.group(1)))
            elif m := _DELETE.search(sql):
                self.log.append(("delete", int(m.group(1))))
                if self.mirror:
                    self.target.discard(int(m.group(1)))
        return SimpleNamespace(run=run, query=None)

    def reader(self):
        def query(sql, params=None, remaining=10.0):
            assert "CAST(id AS text) = %s" in sql and '"qatarget"."t123456789_tgt"' in sql
            self.log.append(("count", params[0]))
            return [(1 if int(params[0]) in self.target else 0,)]
        return SimpleNamespace(query=query, run=None)


def _spec(**over):
    block = {"version": 1, "mode": "cdc", "sink": "db", "readiness": {"kind": "sentinel"},
             "completion": {"kind": "sentinel"}, "stability": 0, "deadlines": {"readiness": 3, "completion": 3},
             "sentinel": {"db": "postgres-source", "insert": "i.sql", "delete": "d.sql",
                          "observe": {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt", "key": "id"}}}
    block.update(over)
    return parse_spec(block, "p")


@pytest.fixture
def pipe(monkeypatch):
    p = Pipeline()
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: admin.probe)
    monkeypatch.setattr(lifecycle, "status_bounded", lambda client, app, remaining: "RUNNING")
    return p


def _admins(p):
    return {"postgres-source": {"admin": SimpleNamespace(probe=p.writer())},
            "postgres-target": {"admin": SimpleNamespace(probe=p.reader())}}


IDENT = runident.derive("lc-sentinel", {"SLT_RUN_EPOCH": "20260914T230000Z-abcd1234"}, attempt="0a1b2c3d")


def _run(p, phase="ready", id_value=4242, seconds=3.0):
    ids = {}
    s = lifecycle.Sentinel(_spec(), _admins(p), TOKENS, IDENT, phase, ids, SQL.__getitem__, lambda: id_value)
    watch = lifecycle.Watch("sentinel", "c", lifecycle.Deadline(seconds, FakeClock()))
    return s, s.run(watch), watch


def test_ready_sentinel_insert_present_delete_absent_order(pipe):
    s, reason, watch = _run(pipe)
    assert reason == "satisfied"
    kinds = [e[0] for e in pipe.log]
    assert kinds[0] == "insert" and kinds.index("delete") > kinds.index("count")
    first_delete = kinds.index("delete")
    assert any(e == ("count", "4242") for e in pipe.log[:first_delete])      # present observed first
    assert pipe.log[-1] == ("count", "4242") and 4242 not in pipe.target      # then absent
    steps = [o["value"].get("step") for o in watch.observations if isinstance(o["value"], dict)]
    assert steps.index("insert") < steps.index("present") < steps.index("delete") < steps.index("absent")


def test_done_sentinel_requires_presence_before_delete_is_submitted(pipe):
    pipe.mirror = False                       # capture suppressed: the target stays empty
    s, reason, watch = _run(pipe, phase="done")
    assert reason == "deadline"
    assert ("delete", 4242) not in pipe.log   # an empty target (count 0 == "absent") never satisfies it


def test_prior_attempt_sentinel_value_is_not_a_match(pipe):
    pipe.target = {777}                       # left behind by a previous attempt
    pipe.mirror = False
    s, reason, _ = _run(pipe, id_value=778)
    assert reason == "deadline" and ("delete", 778) not in pipe.log
    assert {e[1] for e in pipe.log if e[0] == "count"} == {"778"}


def test_sibling_run_value_is_not_a_match(pipe):
    pipe.mirror = False
    sibling = runident.derive("lc-sentinel", {"SLT_RUN_EPOCH": "another-run"}, attempt="ffffffff")
    ids = {}
    other = lifecycle.Sentinel(_spec(), _admins(pipe), TOKENS, sibling, "done", ids, SQL.__getitem__, lambda: 555)
    pipe.target = {other.id}                  # the sibling's sentinel is visible in a shared schema
    s, reason, _ = _run(pipe, phase="done", id_value=556)
    assert reason == "deadline" and s.tag != other.tag


def test_ids_are_in_signed_int_range_and_differ_per_phase_and_attempt(pipe):
    samples = {lifecycle.new_sentinel_id() for _ in range(2000)}
    assert min(samples) >= 1 and max(samples) <= 2 ** 31 - 1 and len(samples) > 1990
    ids = {}
    ready = lifecycle.Sentinel(_spec(), _admins(pipe), TOKENS, IDENT, "ready", ids, SQL.__getitem__)
    done = lifecycle.Sentinel(_spec(), _admins(pipe), TOKENS, IDENT, "done", ids, SQL.__getitem__)
    assert ready.id != done.id and ids == {"ready": ready.id, "done": done.id}
    assert done.tokens["SENTINEL_READY_ID"] == str(ready.id) and done.tokens["SENTINEL_DONE_ID"] == str(done.id)
    assert done.tokens["SENTINEL_ID"] == str(done.id)
    retry = runident.derive("lc-sentinel", {"SLT_RUN_EPOCH": IDENT.run_id})
    again = lifecycle.Sentinel(_spec(), _admins(pipe), TOKENS, retry, "ready", {}, SQL.__getitem__)
    assert again.tag != ready.tag


def test_tag_shape(pipe):
    s = lifecycle.Sentinel(_spec(), _admins(pipe), TOKENS, IDENT, "ready", {}, SQL.__getitem__)
    assert s.tag == f"slt-{IDENT.per_test}-0a1b2c3d-ready" and re.fullmatch(r"slt-t[0-9a-f]{9}-[0-9a-f]{8}-ready", s.tag)
    assert s.tokens["SENTINEL_TAG"] == s.tag


def test_never_arrives_fails_at_deadline(pipe, tmp_path):
    pipe.mirror = False
    (tmp_path / "i.sql").write_text(SQL["i.sql"])
    (tmp_path / "d.sql").write_text(SQL["d.sql"])
    state = lifecycle.State("cdc")
    with pytest.raises(LifecycleError, match="completion sentinel failed: deadline"):
        lifecycle.complete(state, _spec(), client=SimpleNamespace(), apps=["NS.a"], admins=_admins(pipe),
                           tokens=TOKENS, ident=IDENT, read_files=None, source_dir=tmp_path, clock=FakeClock(),
                           owned=lambda kind, db, name: True)
    assert state.completion["kind"] == "sentinel" and state.completion["reason"] == "deadline"
    assert state.sentinel_ids["done"] and state.completion["witness"] is None


def test_insert_failure_is_recorded_not_masked(pipe):
    pipe.writer_error = RuntimeError("permission denied for table t123456789_src")
    s, reason, watch = _run(pipe)
    assert reason.startswith("error:sentinel insert failed: permission denied")
    assert any("permission denied" in str(o["value"]) for o in watch.observations)
