"""Progress faults must land inside a declared window, without live transports."""
import pytest
from livetest.manifest import _normalize_recover, ManifestError


def spec(**extra):
    return dict(kind="target-count", db="postgres-target", table="qatarget.rows",
                above=2, below=10, timeout="1s", poll="100ms", **extra)


def test_manifest_accepts_trigger_and_keeps_delay_defaults():
    got = _normalize_recover({"mode": "stop", "trigger": spec()}, "test.yaml")
    assert got["trigger"]["timeout"] == 1
    assert got["after"] == 0


@pytest.mark.parametrize("update", [{"bogus": 1}, {"above": True}, {"below": 2},
    {"timeout": 0}, {"poll": 0}, {"db": "oracle"}, {"kind": "guess"}])
def test_invalid_trigger_refused(update):
    with pytest.raises(ManifestError, match="invalid-progress-trigger"):
        _normalize_recover({"mode": "stop", "trigger": {**spec(), **update}}, "test.yaml")


def test_trigger_and_after_refused():
    with pytest.raises(ManifestError, match="invalid-progress-trigger"):
        _normalize_recover({"mode": "stop", "after": 1, "trigger": spec()}, "test.yaml")


class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds


def test_wait_uses_open_count_window():
    from livetest.progress import normalize, wait
    clock = Clock(); values = iter([0, 2, 3])
    found = wait(normalize(spec()), lambda left: next(values), now=clock.now, sleep=clock.sleep)
    assert found["value"] == 3 and found["held"] is True
    assert found["observedAt"] == pytest.approx(.2)


@pytest.mark.parametrize("values", [[10], [11], [0]*20])
def test_finished_or_never_started_does_not_fault(values):
    from livetest.progress import normalize, wait, ProgressError
    clock = Clock(); values = iter(values)
    with pytest.raises(ProgressError):
        wait(normalize(spec()), lambda left: next(values), now=clock.now, sleep=clock.sleep)


def test_late_read_cannot_satisfy():
    from livetest.progress import normalize, wait, ProgressError
    clock = Clock()
    def read(left): clock.t += 2; return 3
    with pytest.raises(ProgressError):
        wait(normalize(spec()), read, now=clock.now, sleep=clock.sleep)


def test_checkpoint_advance_uses_selected_numeric_field():
    from livetest.progress import normalize, checkpoint_position
    s = normalize({"kind": "checkpoint", "timeout": "2s"})
    assert s["field"] == "serialNo"
    assert checkpoint_position([{"serialNo": 1}, {"serialNo": 3}], s["field"]) == 3
    with pytest.raises(ValueError): checkpoint_position([{"different": 3}], "serialNo")


def test_lsn_keeps_full_64_bit_position():
    from livetest.progress import lsn
    assert lsn("1/FFFFFFFF") == 8589934591
    with pytest.raises(ValueError): lsn("1/100000000")


def test_reader_checks_ownership_before_database_read():
    from livetest.progress import normalize, reader, ProgressError
    with pytest.raises(ProgressError, match="progress-target-not-owned"):
        reader(normalize(spec()), admins={}, tokens={}, client=None, app="N.A",
               owned=lambda *args: False)


def test_kafka_offset_reads_and_closes_consumer(monkeypatch):
    from livetest.progress import kafka_position
    from types import SimpleNamespace
    calls = []
    class Consumer:
        def get_watermark_offsets(self, tp, timeout, cached):
            calls.append((tp.topic, tp.partition, timeout, cached)); return 0, 12
        def committed(self, tps, timeout):
            return [SimpleNamespace(offset=7, error=None)]
        def close(self): calls.append("closed")
    admin = SimpleNamespace(_consumer=lambda group: Consumer())
    s = {"topic": "events", "partition": 1, "kind": "kafka-end"}
    assert kafka_position(admin, s, 1) == 12
    assert calls[-1] == "closed"
    assert kafka_position(admin, {**s, "kind": "kafka-consumer", "group": "reader"}, 1) == 7


def test_postgres_count_and_slot_use_bounded_parameterized_reads(monkeypatch):
    from livetest.progress import normalize, reader
    import livetest.lifecycle as lc
    calls = []
    class Probe:
        def __init__(self, admin): pass
        def query(self, sql, params=None, remaining=10):
            calls.append((sql, params, remaining))
            return [("1/FFFFFFFF",)] if "pg_replication_slots" in sql else [(4,)]
    monkeypatch.setattr(lc, "PgProbe", Probe)
    kw = dict(admins={"postgres-target": {"admin": object()}}, tokens={}, client=None,
              app="N.A", owned=lambda *args: True)
    assert reader(normalize(spec()), **kw)(.5) == 4
    assert calls[-1] == ('SELECT count(*) FROM "qatarget"."rows"', None, .5)
    s = normalize(dict(kind="postgres-slot", db="postgres-target", slot="owned_slot", above="0/10"))
    assert reader(s, **kw)(.2) == 8589934591
    assert calls[-1][1:] == (("owned_slot",), .2)


@pytest.mark.parametrize("route", ["spanner-google", "spanner-postgres"])
def test_spanner_count_snapshot_closes_and_has_rpc_deadline(route):
    from livetest.progress import normalize, reader
    from types import SimpleNamespace
    calls = []
    class Snapshot:
        def __enter__(self): return self
        def __exit__(self, *args): calls.append("closed")
        def execute_sql(self, sql, timeout): calls.append((sql, timeout)); return [(4,)]
    admin = SimpleNamespace(_db=lambda: SimpleNamespace(snapshot=Snapshot))
    s = normalize({**spec(), "db": route, "table": "rows"})
    read = reader(s, admins={route: {"admin": admin}}, tokens={}, client=None,
                  app="N.A", owned=lambda *args: True)
    assert read(.3) == 4 and calls[-1] == "closed"
    assert calls[0][1] == .3


def test_status_cannot_use_up_deadline_then_send_read():
    from livetest.progress import normalize, wait, ProgressError
    clock = Clock()
    def status(): clock.t += 2; return "RUNNING"
    with pytest.raises(ProgressError):
        wait(normalize(spec()), lambda left: pytest.fail("read after deadline"),
             now=clock.now, sleep=clock.sleep, status=status)


@pytest.mark.parametrize("kind", ["postgres-slot", "kafka-end", "kafka-consumer"])
def test_progress_reader_uses_real_confirmed_ledger(kind, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from livetest import progress, ownership, lifecycle
    ledger = ownership.Ledger(tmp_path / "ledger.json", SimpleNamespace(attempt="current"),
                              {"entries": []}, env={})
    calls = []
    class Probe:
        def __init__(self, admin): pass
        def query(self, sql, params=None, remaining=10):
            calls.append((sql, params, remaining)); return [("0/20",)]
    class Consumer:
        def get_watermark_offsets(self, tp, timeout, cached):
            calls.append((tp.topic, tp.partition, timeout)); return (0, 32)
        def committed(self, tps, timeout):
            calls.append((tps[0].topic, tps[0].partition, timeout))
            return [SimpleNamespace(offset=32, error=None)]
        def close(self): pass
    monkeypatch.setattr(lifecycle, "PgProbe", Probe)
    if kind == "postgres-slot":
        db, resource, name = "postgres-source", "pg-slot", "case_slot"
        spec = {"kind": kind, "db": db, "slot": "${PG_SLOT}", "above": "0/10"}
    else:
        db, resource, name = "kafka", "kafka-topic", "case_events"
        spec = {"kind": kind, "topic": "${TID}events", "partition": 0, "above": 16}
        if kind == "kafka-consumer": spec["group"] = "${TID}reader"
    entry = ledger.add(resource, name, db, "confirmed")
    admin = SimpleNamespace(_consumer=lambda group: Consumer())
    kwargs = dict(admins={db: {"admin": admin}}, tokens={"PG_SLOT": "case_slot", "TID": "case_"},
                  client=None, app="N.A", owned=ledger.owns)
    normalized = progress.normalize(spec)
    assert progress.reader(normalized, **kwargs)(.5) == 32
    assert len(calls) == 1
    for field, value in [("state", "intended"), ("attempt", "prior"), ("db", "other-route")]:
        original = entry[field]; entry[field] = value
        with pytest.raises(progress.ProgressError, match="progress-target-not-owned"):
            progress.reader(normalized, **kwargs)
        assert len(calls) == 1
        entry[field] = original
