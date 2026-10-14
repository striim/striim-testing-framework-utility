from types import SimpleNamespace

import pytest

from livetest import snapshot_observer as observer, canon
from livetest.assertions import AssertionFailed


def spec():
    return {"db": "postgres-target", "tables": {
        "a": {"target": "${PG_TARGET_SCHEMA}.${TID}a", "columns": {"id": "integer"}},
        "b": {"target": "${PG_TARGET_SCHEMA}.${TID}b", "columns": {"id": "integer"}}},
        "prefixes": [{"a": "empty.csv", "b": "empty.csv"}, {"a": "full.csv", "b": "full.csv"}],
        "interval": "10ms"}


def make_observer(rows):
    data = {"empty.csv": b"id\n", "full.csv": b"id\n1\n"}
    inputs = SimpleNamespace(golden=lambda p: data[p.name])
    return observer.Observer(spec(), admins={}, tokens={}, case_dir=".", inputs=inputs,
                             read=lambda *a: rows)


def test_whole_snapshot_matches_a_prefix():
    o = make_observer({"a": [{"id": 1}], "b": [{"id": 1}]})
    o.sample()
    assert o.history[0]["prefix"] == 1


def test_partial_transaction_is_rejected():
    o = make_observer({"a": [{"id": 1}], "b": []})
    with pytest.raises(AssertionFailed, match="snapshot-not-committed-prefix"):
        o.sample()


def test_duplicate_is_rejected():
    o = make_observer({"a": [{"id": 1}, {"id": 1}], "b": [{"id": 1}]})
    with pytest.raises(AssertionFailed, match="snapshot-not-committed-prefix"):
        o.sample()


@pytest.mark.parametrize("change", [lambda s: s.update(db="mysql-target"), lambda s: s.update(unknown=1),
    lambda s: s.update(interval=0), lambda s: s["prefixes"][0].pop("b"),
    lambda s: s["prefixes"][0].update(a="../escape.csv")])
def test_bad_snapshot_specs_are_refused(change):
    s = spec(); change(s)
    with pytest.raises(ValueError, match="invalid-snapshot-spec"):
        observer.normalize(s, "p", lifecycle=True)


def test_pg_one_connection_one_repeatable_read(monkeypatch):
    from livetest import lifecycle
    calls = []
    connection = object()
    class Probe:
        def _call(self, sql, params, remaining, fetch, read):
            return read(connection, lambda: False)
    monkeypatch.setattr(lifecycle, "PgProbe", lambda admin: Probe())
    def read(conn, table, order, rows, bytes_, cancelled, **kw):
        calls.append((conn, kw["start_snapshot"]))
        return ["id"], [(1,)]
    monkeypatch.setattr(lifecycle, "exact_read", read)
    result = observer.read_snapshot(spec(), {"postgres-target": {"admin": object()}},
                                    {"PG_TARGET_SCHEMA": "qatarget", "TID": "case_"}, 2)
    assert result == {"a": [{"id": 1}], "b": [{"id": 1}]}
    assert calls == [(connection, True), (connection, False)]


def test_spanner_one_multi_use_snapshot(monkeypatch):
    from livetest import exactdata
    calls = []
    class Snapshot:
        def __enter__(self): return self
        def __exit__(self, *a): calls.append("closed")
    snapshot = Snapshot()
    class Database:
        def snapshot(self, **kw):
            assert kw == {"multi_use": True}
            calls.append("opened"); return snapshot
    admin = SimpleNamespace(_db=lambda: Database())
    def read(*a, **kw):
        assert kw["snapshot"] is snapshot
        calls.append("read"); return [{"id": 1}]
    monkeypatch.setattr(exactdata, "read_spanner", read)
    s = spec(); s["db"] = "spanner-google"
    observer.read_snapshot(s, {"spanner-google": {"admin": admin}},
                           {"PG_TARGET_SCHEMA": "qatarget", "TID": "case_"}, 2)
    assert calls == ["opened", "read", "read", "closed"]
