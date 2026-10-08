"""Bounded, monotonic lifecycle witnesses (C7.2-C7.3).

A missing readiness or completion signal reaches a bounded failure with its record; the
initial-load readiness is the landed baseline, not a stable source; Postgres probes carry
connect and statement timeouts and are cancelled, closed and never reused on expiry; the
RUNNING wait of a lifecycle case is bounded. Fake clock, fake probes and fake clients only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from livetest import lifecycle
from livetest.lifecycle import LifecycleError, parse_spec

TOKENS = {"PG_SOURCE_SCHEMA": "qasource", "PG_TARGET_SCHEMA": "qatarget", "TID": "t123456789_",
          "PG_SLOT": "slt_t123456789", "OWNED_DIR": "/opt/striim/slt-runs/SLT_lc_t123456789"}
SRC = {"db": "postgres-source", "table": "${PG_SOURCE_SCHEMA}.${TID}src"}
TGT = {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt"}


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.n = 0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(float(s), 0.001)

    def wall(self):
        self.n += 1
        return f"2026-09-14T00:00:00.{self.n:03d}Z"


class Probe:
    def __init__(self, fn):
        self.fn = fn

    def query(self, sql, params=None, remaining=10.0):
        return self.fn(sql, params)

    def run(self, sql, remaining=10.0):
        return self.fn(sql, None)


class Client:
    def __init__(self, statuses):
        self.statuses = statuses          # callable(app) -> status

    def await_running(self, *a, **k):
        raise AssertionError("the lifecycle path must never use the unbounded await_running")


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: admin.probe)
    monkeypatch.setattr(lifecycle, "status_bounded", lambda client, app, remaining: client.statuses(app))


def _admins(source_fn=None, target_fn=None):
    return {"postgres-source": {"admin": SimpleNamespace(probe=Probe(source_fn or (lambda s, p: [(0,)])))},
            "postgres-target": {"admin": SimpleNamespace(probe=Probe(target_fn or (lambda s, p: [(0,)])))}}


def _counts(*values):
    it = iter(values)
    last = {"v": values[-1]}

    def fn(sql, params):
        last["v"] = next(it, last["v"])
        return [(last["v"],)]
    return fn


def _spec(**over):
    block = {"version": 1, "mode": "cdc", "sink": "db",
             "readiness": {"kind": "source-progress", "db": "postgres-source"},
             "completion": {"kind": "row-count", "db": "postgres-target", "table": TGT["table"], "expect": 3},
             "stability": 0, "deadlines": {"readiness": 5, "completion": 5}}
    block.update(over)
    return parse_spec(block, "p")


def _initial(**over):
    block = {"version": 1, "mode": "initial-load", "sink": "db",
             "readiness": {"kind": "baseline-landed", "source": SRC, "target": TGT},
             "completion": {"kind": "source-count", "source": SRC, "target": TGT},
             "stability": 0, "deadlines": {"readiness": 5, "completion": 5}}
    block.update(over)
    return parse_spec(block, "p")


def _owned(kind, db, name):
    return True          # witness ownership is proven against a real ledger in test_ownership


def _kw(clock, admins, statuses=lambda a: "RUNNING", read_files=lambda p, **k: ""):
    return dict(client=Client(statuses), apps=["NS.lcApp"], admins=admins, tokens=TOKENS, ident=None,
                read_files=read_files, clock=clock, owned=_owned)


def test_readiness_never_satisfied_fails_at_deadline_with_record():
    clock, state = FakeClock(), lifecycle.State("cdc")
    admins = _admins(source_fn=lambda s, p: [(False,)])
    with pytest.raises(LifecycleError, match="readiness source-progress failed: deadline") as ei:
        lifecycle.smoke_and_ready(state, _spec(), **_kw(clock, admins))
    rec = state.ready
    assert rec is ei.value.record and rec["reason"] == "deadline" and rec["witness"] is None
    assert rec["deadlineS"] == 5.0 and rec["observations"] and rec["observations"][-1]["value"] is False
    assert clock.t - 1000.0 >= 5.0 and clock.t - 1000.0 < 7.0          # bounded, not open-ended
    assert ei.value.records[0]["type"] == "smoke" and ei.value.records[0]["status"] == "passed"


def test_completion_never_satisfied_fails_at_deadline():
    clock, state = FakeClock(), lifecycle.State("cdc")
    admins = _admins(target_fn=lambda s, p: [(1,)])
    with pytest.raises(LifecycleError, match="completion row-count failed: deadline"):
        lifecycle.complete(state, _spec(), **_kw(clock, admins))
    assert state.completion["reason"] == "deadline" and state.completion["observations"][-1]["value"] == 1


def test_terminal_status_fails_early():
    clock, state = FakeClock(), lifecycle.State("cdc")
    seen = {"n": 0}

    def statuses(app):
        seen["n"] += 1
        return "RUNNING" if seen["n"] <= 3 else "HALT"

    admins = _admins(target_fn=lambda s, p: [(1,)])
    with pytest.raises(LifecycleError, match="terminal-status:HALT"):
        lifecycle.complete(state, _spec(deadlines={"readiness": 5, "completion": 600}), **_kw(clock, admins, statuses))
    assert state.completion["reason"].startswith("terminal-status:HALT")
    assert clock.t - 1000.0 < 60.0                                     # long before the 600 s deadline


def test_stability_lost_fails():
    clock, state = FakeClock(), lifecycle.State("cdc")
    admins = _admins(target_fn=_counts(3, 2))
    with pytest.raises(LifecycleError, match="stability-lost"):
        lifecycle.complete(state, _spec(stability="2s"), **_kw(clock, admins))
    assert state.stability["held"] is False and state.stability["seconds"] == 2.0 and state.stability["value"] == 2


def test_baseline_landed_requires_sink_equal_positive_baseline_and_running():
    clock, state = FakeClock(), lifecycle.State("initial-load")
    spec = _initial()
    admins = _admins(source_fn=lambda s, p: [(3,)], target_fn=_counts(0, 1, 3))
    lifecycle.record_baseline(state, spec, admins, TOKENS, clock=clock, owned=_owned)
    assert state.baseline["count"] == 3 and state.baseline["table"] == '"qasource"."t123456789_src"'
    smoke = lifecycle.smoke_and_ready(state, spec, **_kw(clock, admins))
    assert smoke[0]["status"] == "passed"
    assert state.running["reason"] == "satisfied"
    assert state.ready["reason"] == "satisfied" and "baseline 3" in state.ready["witness"]
    assert [o["value"] for o in state.ready["observations"]] == [0, 1, 3]
    # not RUNNING: the baseline cannot be taken as readiness
    state2 = lifecycle.State("initial-load")
    state2.baseline = dict(state.baseline)
    with pytest.raises(LifecycleError, match="deadline before RUNNING"):
        lifecycle.smoke_and_ready(state2, spec, **_kw(FakeClock(), _admins(target_fn=lambda s, p: [(3,)]),
                                                     statuses=lambda a: "STARTING"))
    assert state2.ready["reason"] == "deadline" and ei_smoke_failed(state2)


def ei_smoke_failed(state):
    return state.running["reason"] == "deadline"


def test_baseline_landed_stalled_reader_times_out():
    # RUNNING, source stable at a positive baseline, sink never reaches it (a suppressed reader).
    clock, state = FakeClock(), lifecycle.State("initial-load")
    spec = _initial()
    admins = _admins(source_fn=lambda s, p: [(3,)], target_fn=lambda s, p: [(0,)])
    lifecycle.record_baseline(state, spec, admins, TOKENS, clock=clock, owned=_owned)
    with pytest.raises(LifecycleError, match="readiness baseline-landed failed: deadline"):
        lifecycle.smoke_and_ready(state, spec, **_kw(clock, admins))
    assert state.running["reason"] == "satisfied" and state.ready["reason"] == "deadline"
    assert {o["value"] for o in state.ready["observations"]} == {0}


def test_source_count_zero_zero_fails_with_zero_count():
    clock, state = FakeClock(), lifecycle.State("initial-load")
    admins = _admins(source_fn=lambda s, p: [(0,)], target_fn=lambda s, p: [(0,)])
    with pytest.raises(LifecycleError, match="zero-count"):
        lifecycle.record_baseline(state, _initial(), admins, TOKENS, clock=clock, owned=_owned)
    assert state.ready["reason"] == "zero-count" and state.baseline["count"] == 0
    state2 = lifecycle.State("initial-load")
    spec = _initial(readiness={"kind": "baseline-landed", "source": SRC, "target": TGT})
    with pytest.raises(LifecycleError, match="completion source-count failed: zero-count"):
        lifecycle.complete(state2, spec, **_kw(FakeClock(), admins))
    assert state2.completion["reason"] == "zero-count"


def test_row_count_and_file_lines_positive_and_stable():
    clock, state = FakeClock(), lifecycle.State("cdc")
    rec = lifecycle.complete(state, _spec(stability="1s"), **_kw(clock, _admins(target_fn=_counts(1, 3, 3))))
    assert rec["reason"] == "satisfied" and state.stability["held"] is True and state.stability["value"] == 3
    fspec = parse_spec({"version": 1, "mode": "cdc", "sink": "file",
                        "readiness": {"kind": "source-progress", "db": "postgres-source"},
                        "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 3},
                        "stability": "1s", "deadlines": {"readiness": 5, "completion": 5}}, "p")
    read = []
    state2 = lifecycle.State("cdc")
    rec2 = lifecycle.complete(state2, fspec, **_kw(FakeClock(), _admins(),
                                                   read_files=lambda p, **k: read.append(p) or "a\nb\n\nc\n"))
    assert rec2["reason"] == "satisfied" and read[0] == "/opt/striim/slt-runs/SLT_lc_t123456789/out.csv"


def _file_spec(completion_s):
    return parse_spec({"version": 1, "mode": "cdc", "sink": "file",
                       "readiness": {"kind": "source-progress", "db": "postgres-source"},
                       "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 3},
                       "stability": 0, "deadlines": {"readiness": 5, "completion": completion_s}}, "p")


class _JumpClock(FakeClock):
    """A FakeClock whose next sleep can land at a set time (the wait's last poll before its deadline)."""
    jump_to = None

    def sleep(self, s):
        super().sleep(s)
        if self.jump_to is not None:
            self.t, self.jump_to = max(self.t, self.jump_to), None


def test_last_file_read_cut_off_by_the_deadline_reports_deadline_not_probe_hung():
    # 03 read its file every ~1.3 s for 60 s (never the baseline), and the last read,
    # started with a fraction of a second left, was killed at the deadline: reported probe-hung.
    # Here each read takes 0.2 s and the last one starts with 0.3 s left.
    clock, reads = _JumpClock(), []

    def read_files(path, run=None):
        reads.append(path)
        clock.t += 0.2 if len(reads) < 4 else 0.3
        if len(reads) == 3:
            clock.jump_to = 1000.0 + 2 - 0.3          # the completion deadline starts at t=1000, 2 s long
        if len(reads) == 4:                           # the remaining 0.3 s run out while this read is in flight
            raise lifecycle.ProbeHung("file read did not finish within the deadline")
        return "a\n"
    state = lifecycle.State("cdc")
    with pytest.raises(LifecycleError, match="failed: deadline"):
        lifecycle.complete(state, _file_spec(2), **_kw(clock, _admins(), read_files=read_files))
    rec = state.completion
    assert rec["reason"] == "deadline", rec
    assert [o["value"] for o in rec["observations"]] == [1, 1, 1, {"error": "file read did not finish within the deadline"}]


def test_a_read_that_hangs_after_a_good_read_is_still_probe_hung():
    # Read 1 returns at once, read 2 wedges (a stuck docker exec) and is
    # killed only when the whole 60 s deadline is spent: the witness hung, the app was not merely late
    clock, reads = FakeClock(), []

    def read_files(path, run=None):
        reads.append(path)
        if len(reads) == 2:
            clock.t = 1000.0 + 60                     # the read ran for the rest of the deadline
            raise lifecycle.ProbeHung("file read killed after 58.8s")
        return "a\n"
    state = lifecycle.State("cdc")
    with pytest.raises(LifecycleError, match="failed: probe-hung"):
        lifecycle.complete(state, _file_spec(60), **_kw(clock, _admins(), read_files=read_files))
    assert state.completion["reason"] == "probe-hung"
    assert [o["value"] for o in state.completion["observations"]] == [1, {"error": "file read killed after 58.8s"}]


def test_bounded_probe_returns_within_remaining_when_fn_blocks():
    gate = threading.Event()
    started = time.monotonic()
    with pytest.raises(lifecycle.ProbeHung):
        lifecycle.bounded(lambda: gate.wait(30), 0.2)
    assert time.monotonic() - started < 2.0
    gate.set()
    assert lifecycle.bounded(lambda: 7, 1.0) == 7
    with pytest.raises(ZeroDivisionError):
        lifecycle.bounded(lambda: 1 / 0, 1.0)


def test_status_bounded_passes_timeout_to_requests(monkeypatch):
    monkeypatch.undo()                     # the real status_bounded
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return SimpleNamespace(json=lambda: {"status": "RUNNING"})

    api = SimpleNamespace(url_base="http://localhost:9080", getHeader=lambda kind: {"Accept": kind, "Authorization": "t"})
    assert lifecycle.status_bounded(SimpleNamespace(api=api), "NS.app", 3.5, get=get) == "RUNNING"
    assert calls == [("http://localhost:9080/api/v2/applications/NS.app",
                      {"Accept": "application/json", "Authorization": "t"}, 3.5)]
    lifecycle.status_bounded(SimpleNamespace(api=api), "NS.app", 120.0, get=get)
    assert calls[-1][2] == 10.0 and lifecycle.status_bounded(SimpleNamespace(api=api), "a", 0.0, get=get)


def test_deadline_monotonic_ignores_wall_clock(monkeypatch):
    clock = FakeClock()
    jumps = iter(["2030-01-01T00:00:00Z", "1999-01-01T00:00:00Z"] * 100)
    clock.wall = lambda: next(jumps)                 # the wall clock jumps both ways
    monkeypatch.setattr(time, "time", lambda: 0.0)
    d = lifecycle.Deadline(5, clock)
    assert d.remaining() == 5.0 and not d.expired()
    clock.t += 4.9
    assert not d.expired()
    clock.t += 0.2
    assert d.expired() and d.remaining() == 0.0
    state = lifecycle.State("cdc")
    with pytest.raises(LifecycleError):
        lifecycle.complete(state, _spec(), **_kw(FakeClock(), _admins(target_fn=lambda s, p: [(1,)])))
    assert state.completion["deadlineS"] == 5.0


def test_observations_bounded_to_50():
    clock, state = FakeClock(), lifecycle.State("cdc")
    with pytest.raises(LifecycleError):
        lifecycle.complete(state, _spec(deadlines={"readiness": 5, "completion": 200}),
                           **_kw(clock, _admins(target_fn=_counts(*range(4, 1000)))))
    assert len(state.completion["observations"]) == lifecycle.MAX_OBSERVATIONS == 50


def test_records_keep_c4_witness_and_at():
    clock, state = FakeClock(), lifecycle.State("cdc")
    rec = lifecycle.complete(state, _spec(), **_kw(clock, _admins(target_fn=lambda s, p: [(3,)])))
    assert tuple(rec) == lifecycle.RECORD_KEYS
    assert isinstance(rec["witness"], str) and rec["witness"] and rec["at"] == rec["endedAt"]
    assert rec["condition"] == '"qatarget"."t123456789_tgt" count == 3'


class _Conn:
    """A psycopg2-shaped connection whose execute blocks until cancel()."""

    def __init__(self, cancel_raises=False):
        self.cancelled = threading.Event()
        self.closed = False
        self.mutated = False
        self.cancel_raises = cancel_raises
        self.autocommit = False

    def cursor(self):
        conn = self

        class Cur:
            description = None

            def execute(self, sql, params=None):
                conn.cancelled.wait(30)
                if conn.cancelled.is_set():
                    raise RuntimeError("canceling statement due to user request")
                conn.mutated = True

            def fetchall(self):
                return []

            def close(self):
                pass
        return Cur()

    def cancel(self):
        if self.cancel_raises:
            raise OSError("cancel request failed")
        self.cancelled.set()

    def close(self):
        self.closed = True


_DSN = {"host": "localhost", "port": 5432, "dbname": "sltdb", "source_user": "qasource", "source_password": "striim"}


def test_pg_probe_statement_timeout_and_cancel_no_late_mutation():
    conns, kwargs = [], []

    def connect(**kw):
        kwargs.append(kw)
        conns.append(_Conn())
        return conns[-1]

    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"), connect=connect)
    started = time.monotonic()
    with pytest.raises(lifecycle.ProbeCancelled) as ei:
        probe.run("INSERT INTO qasource.t123456789_src (id) VALUES (1)", remaining=0.3)
    assert time.monotonic() - started < 3.0 and ei.value.reason == "probe-cancelled"
    assert kwargs[0]["options"] == "-c statement_timeout=300" and kwargs[0]["connect_timeout"] == 1
    assert kwargs[0]["user"] == "qasource" and kwargs[0]["dbname"] == "sltdb"
    time.sleep(0.2)
    assert conns[0].cancelled.is_set() and conns[0].closed and conns[0].mutated is False


def test_pg_probe_cancel_failed_reason():
    conns = []
    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"),
                              connect=lambda **kw: conns.append(_Conn(cancel_raises=True)) or conns[-1])
    with pytest.raises(lifecycle.ProbeCancelFailed, match="cancel\\(\\) failed") as ei:
        probe.query("SELECT 1", None, remaining=0.2)
    assert ei.value.reason == "cancel-failed" and conns[0].closed
    conns[0].cancelled.set()
    # through a witness: the wait ends with that reason
    clock, state = FakeClock(), lifecycle.State("cdc")
    admins = _admins()
    admins["postgres-source"]["admin"] = SimpleNamespace(probe=SimpleNamespace(
        query=lambda *a, **k: (_ for _ in ()).throw(lifecycle.ProbeCancelFailed("cancel failed"))))
    with pytest.raises(LifecycleError, match="cancel-failed"):
        lifecycle.smoke_and_ready(state, _spec(), **_kw(clock, admins))
    assert state.ready["reason"] == "cancel-failed"


def test_probe_not_reused_after_timeout():
    conns = []
    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"),
                              connect=lambda **kw: conns.append(_Conn()) or conns[-1])
    with pytest.raises(lifecycle.ProbeCancelled):
        probe.query("SELECT count(*) FROM qatarget.t", None, remaining=0.2)
    assert probe.spent
    with pytest.raises(lifecycle.ProbeSpent):
        probe.query("SELECT 1", None, remaining=1.0)
    assert len(conns) == 1                         # no second connection was opened


def test_await_running_bounded_replaces_unbounded_wait(monkeypatch):
    gate = threading.Event()
    client = Client(lambda app: gate.wait(30) and "RUNNING")
    started = time.monotonic()
    rec = lifecycle.await_running_bounded(client, ["NS.lcApp"], lifecycle.Deadline(0.3))
    assert rec["reason"] == "probe-hung" and time.monotonic() - started < 3.0
    gate.set()
    ok = lifecycle.await_running_bounded(Client(lambda app: "RUNNING"), ["NS.a", "NS.b"], lifecycle.Deadline(1))
    assert ok["reason"] == "satisfied" and ok["witness"] == "RUNNING observed for NS.a, NS.b"
    halted = lifecycle.await_running_bounded(Client(lambda app: "CRASH"), ["NS.a"], lifecycle.Deadline(1))
    assert halted["reason"] == "terminal-status:CRASH (NS.a)"


# ---------------------------------------------------------------- code review r1: R4 cancellation, R6 stability

class _WriteConn:
    """Connects late (the caller sleeps before returning it); records every statement it executes."""

    def __init__(self, execute_delay=0.0):
        self.executed, self.closed, self.execute_delay = [], False, execute_delay
        self.autocommit = False

    def cursor(self):
        conn = self

        class Cur:
            description = None

            def execute(self, sql, params=None):
                time.sleep(conn.execute_delay)
                conn.executed.append(sql)

            def fetchall(self):
                return []

            def close(self):
                pass
        return Cur()

    def cancel(self):
        pass                                     # nothing is running on the server yet: a no-op cancel

    def close(self):
        self.closed = True


def test_pg_probe_delayed_connect_never_sends_sql_after_expiry():
    conns = []

    def connect(**kw):
        time.sleep(0.6)                          # connection establishment outlives the 0.3 s remaining
        conns.append(_WriteConn())
        return conns[-1]

    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"), connect=connect)
    started = time.monotonic()
    with pytest.raises(lifecycle.ProbeCancelled):
        probe.run("INSERT INTO qasource.t123456789_src (id) VALUES (1)", remaining=0.3)
    assert time.monotonic() - started < 1.0
    time.sleep(1.0)                              # the late connection has opened by now
    assert len(conns) == 1 and conns[0].executed == [] and conns[0].closed


def test_pg_probe_hanging_cancel_is_bounded_and_reported(monkeypatch):
    monkeypatch.setattr(lifecycle, "CANCEL_BOUND_S", 0.3, raising=False)
    release = threading.Event()

    class HangingCancel(_Conn):
        def cancel(self):
            release.wait(60)

    conns, box = [], {}
    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"),
                              connect=lambda **kw: conns.append(HangingCancel()) or conns[-1])

    def call():
        try:
            probe.query("SELECT count(*) FROM qatarget.t", None, remaining=0.2)
        except Exception as e:                   # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(10)
    alive = t.is_alive()
    release.set()
    conns[0].cancelled.set()
    assert not alive, "a hanging cancel() held the probe past its bound"
    assert isinstance(box["error"], lifecycle.ProbeCancelFailed) and box["error"].reason == "cancel-failed"
    assert "cancel() did not return" in str(box["error"])


def test_pg_probe_statement_completing_after_the_deadline_is_cancel_failed(monkeypatch):
    monkeypatch.setattr(lifecycle, "CANCEL_BOUND_S", 2.0, raising=False)
    conns = []
    probe = lifecycle.PgProbe(SimpleNamespace(dsn=_DSN, role="source"),
                              connect=lambda **kw: conns.append(_WriteConn(execute_delay=0.5)) or conns[-1])
    with pytest.raises(lifecycle.ProbeCancelFailed, match="completed after the deadline"):
        probe.run("INSERT INTO qasource.t123456789_src (id) VALUES (1)", remaining=0.2)
    assert conns[0].executed                     # a late write is never reported as a clean cancellation


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_file_read_process_is_killed_at_the_deadline(tmp_path):
    pidfile = tmp_path / "pid"
    script = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(30)"

    def read_files(path, run=None):              # read_server_files' shape: one docker command per node with output
        run = run or (lambda argv: subprocess.run(argv, capture_output=True, text=True))
        return run([sys.executable, "-c", script]).stdout

    spec = parse_spec({"version": 1, "mode": "cdc", "sink": "file",
                       "readiness": {"kind": "source-progress", "db": "postgres-source"},
                       "completion": {"kind": "file-lines", "path": "${OWNED_DIR}/out.csv", "lines": 3},
                       "stability": 0, "deadlines": {"readiness": 5, "completion": 1}}, "p")
    state = lifecycle.State("cdc")
    started = time.monotonic()
    with pytest.raises(LifecycleError, match="probe-hung"):
        lifecycle.complete(state, spec, **_kw(FakeClock(), _admins(), read_files=read_files))
    assert time.monotonic() - started < 10
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "the reader process outlived the witness deadline"


def _sentinel_spec(phase):
    sentinel = {"db": "postgres-source", "insert": "ins.sql", "delete": "del.sql",
                "observe": {"db": "postgres-target", "table": "${PG_TARGET_SCHEMA}.${TID}tgt", "key": "id"}}
    readiness = {"kind": "sentinel"} if phase == "ready" else {"kind": "source-progress", "db": "postgres-source"}
    return parse_spec({"version": 1, "mode": "cdc", "sink": "db", "readiness": readiness,
                       "completion": {"kind": "sentinel"}, "sentinel": sentinel, "stability": "5s",
                       "deadlines": {"readiness": 30, "completion": 30}}, "p")


def _sentinel_world(tmp_path, clock, reappear):
    (tmp_path / "ins.sql").write_text("INSERT ${SENTINEL_ID}")
    (tmp_path / "del.sql").write_text("DELETE ${SENTINEL_ID}")
    target = {"rows": set(), "deleted_at": None}

    def writer(sql, params):
        verb, sid = sql.split()
        if verb == "INSERT":
            target["rows"].add(sid)
        else:
            target["rows"].discard(sid)
            target["deleted_at"] = clock.t
        return []

    def reader(sql, params):
        if reappear and target["deleted_at"] is not None and clock.t >= target["deleted_at"] + 2:
            target["rows"].add(params[0])        # the replicated row comes back during the interval
        return [(1 if params[0] in target["rows"] else 0,)]
    return _admins(source_fn=writer, target_fn=reader)


@pytest.mark.parametrize("phase", ["ready", "done"])
def test_sentinel_stability_rereads_the_sentinel_after_the_interval(tmp_path, phase):
    clock = FakeClock()
    admins = _sentinel_world(tmp_path, clock, reappear=True)
    state, spec = lifecycle.State("cdc"), _sentinel_spec(phase)
    call = lifecycle.smoke_and_ready if phase == "ready" else lifecycle.complete
    with pytest.raises(LifecycleError, match="stability-lost"):
        call(state, spec, source_dir=tmp_path, id_source=lambda: 42, **_kw(clock, admins))
    assert state.stability["held"] is False and state.stability["value"] == 1
    assert state.stability["measuredS"] >= 5.0

    clock2 = FakeClock()
    state2 = lifecycle.State("cdc")
    call(state2, spec, source_dir=tmp_path, id_source=lambda: 42, **_kw(clock2, _sentinel_world(tmp_path, clock2, reappear=False)))
    assert state2.stability["held"] is True and state2.stability["value"] == 0 and state2.stability["measuredS"] >= 5.0
