from types import SimpleNamespace

import pytest

from livetest import lifecycle, nothing, sinkprobe
from livetest.assertions.file import parse_file_specs
from livetest.spanneradmin import SpannerAdmin
from tests.lifecycle.test_nothing import Clock, spec


@pytest.mark.parametrize("terminal_poll", [1, 2])
def test_absence_preserves_terminal_failure_on_every_poll(terminal_poll):
    class TerminalFailure(RuntimeError):
        pass
    probes = []
    def status():
        probes.append(True)
        if len(probes) == terminal_poll:
            raise TerminalFailure("terminal-status:HALT")
    with pytest.raises(TerminalFailure, match="terminal-status:HALT"):
        nothing.assert_nothing([spec()], kind="data", tokens={"PG_TARGET_SCHEMA": "qatarget", "TID": "test_"},
            completion={"kind": "sentinel", "reason": "satisfied"}, clock=Clock(),
            read=lambda *a: [], status_probe=status)


@pytest.mark.parametrize("route", ["postgres-target", "spanner-google", "spanner-postgres"])
def test_database_sentinel_refuses_dotted_key_at_load(route):
    with pytest.raises(ValueError, match="plain column"):
        sinkprobe.validate_ref({"db": route, "table": "${TID}rows", "key": "a.b"}, "db", "case")


@pytest.mark.parametrize("sentinel", ["foo", [], 2, {"db": "postgres-source", "insert": "i.sql", "delete": "d.sql", "observe": "foo"}])
def test_file_sentinel_typos_are_spec_errors(sentinel):
    with pytest.raises(lifecycle.LifecycleSpecError):
        lifecycle.parse_spec({"version": 1, "mode": "cdc", "sink": "file", "readiness": {"kind": "sentinel"},
            "completion": {"kind": "sentinel"}, "sentinel": sentinel}, "case")


def test_file_parser_accepts_prevalidated_absence_specs():
    check = {"path": "${OWNED_DIR}/rows.json", "nothing": {"window": "1s", "where": {"id": 1}}}
    assert parse_file_specs([check]) == [check]


def test_append_sentinel_renders_deletion_values(monkeypatch):
    ref = {"db": "kafka", "topic": "${KAFKA_TGT_TOPIC}", "key": "value.id", "deletion": {"value.op": "${TID}deleted"}}
    block = {"version": 1, "mode": "cdc", "sink": "kafka", "readiness": {"kind": "sentinel"},
        "completion": {"kind": "sentinel"}, "stability": 0,
        "sentinel": {"db": "postgres-source", "insert": "i.sql", "delete": "d.sql", "observe": ref}}
    values = []
    def write(sql, remaining):
        values.append({"value": {"id": 42, "op": "inserted" if sql == "i.sql" else "test_deleted"}})
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: SimpleNamespace(run=write))
    monkeypatch.setattr(sinkprobe, "rows", lambda *a: list(values))
    sentinel = lifecycle.Sentinel(lifecycle.parse_spec(block, "case"), {"postgres-source": {"admin": object()}},
        {"TID": "test_"}, SimpleNamespace(per_test="case", attempt="attempt"), "done", {},
        lambda name: name, id_source=lambda: 42)
    watch = lifecycle.Watch("sentinel", "delivery", lifecycle.Deadline(2, Clock()))
    assert sentinel.run(watch) == "satisfied"


@pytest.mark.parametrize("dialect,cast,parameter", [("google_standard_sql", "STRING", "@sentinel_id"), ("postgresql", "text", "$1")])
def test_spanner_sentinel_counts_only_its_key(monkeypatch, dialect, cast, parameter):
    calls, counts = [], iter([1, 0])
    class Snapshot:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute_sql(self, sql, **kw):
            calls.append((sql, kw))
            assert "SELECT *" not in sql
            assert f"WHERE CAST(id AS {cast}) = {parameter}" in sql
            assert list(kw["params"].values()) == ["42"]
            assert 0 < kw["timeout"] <= 2
            return [[next(counts)]]
    admin = SpannerAdmin({"dialect": dialect}, database=SimpleNamespace(snapshot=Snapshot))
    route = "spanner-postgres" if dialect == "postgresql" else "spanner-google"
    ref = {"db": route, "table": "${TID}rows", "key": "id"}
    block = {"version": 1, "mode": "cdc", "sink": "db", "readiness": {"kind": "sentinel"},
        "completion": {"kind": "sentinel"}, "stability": 0,
        "sentinel": {"db": "postgres-source", "insert": "i.sql", "delete": "d.sql", "observe": ref}}
    monkeypatch.setattr(lifecycle, "make_probe", lambda admin: SimpleNamespace(run=lambda *a: None))
    sentinel = lifecycle.Sentinel(lifecycle.parse_spec(block, "case"),
        {"postgres-source": {"admin": object()}, route: {"admin": admin}}, {"TID": "test_"},
        SimpleNamespace(per_test="case", attempt="attempt"), "done", {}, lambda name: name, id_source=lambda: 42)
    assert sentinel.run(lifecycle.Watch("sentinel", "keyed count", lifecycle.Deadline(2, Clock()))) == "satisfied"
    assert len(calls) == 2
