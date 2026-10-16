"""Typed route reads preserve values and refuse incomplete or over-budget snapshots."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from livetest import canon, exactdata
from livetest.spanneradmin import SpannerAdmin
from livetest.assertions import AssertionFailed


@pytest.mark.parametrize("route", ["spanner-google", "spanner-postgres", "kafka"])
def test_new_exact_route(route):
    spec = {"target": "${TID}rows", "db": route, "match": "expected/rows.csv",
            "exact": {"columns": {"id": "integer"}}}
    if route == "kafka":
        spec.update(project="kafka_record", keys=["value.id"])
        spec["exact"] = {"columns": {"value.id": "integer"}, "order": "sequence"}
    result = canon.check_manifest({"exact": {"version": 1}, "assert": {"data": [spec]}})
    assert result["specs"][0][2].order == ("sequence" if route == "kafka" else "any")


class Snapshot:
    def __init__(self, rows):
        self.rows, self.calls, self.closed = rows, [], False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute_sql(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        class Result:
            fields = [SimpleNamespace(name="id"), SimpleNamespace(name="amount")]
            def __iter__(inner):
                return iter(self.rows)
        return Result()


def test_spanner_bounded_typed_read():
    snap = Snapshot([(1, Decimal("2.50"))])
    admin = SpannerAdmin({}, database=SimpleNamespace(snapshot=lambda: snap))
    rows = exactdata.read_spanner(admin, "rows", ["id"], 2, 1000, 3)
    assert rows == [{"id": 1, "amount": Decimal("2.50")}]
    assert snap.closed
    sql, kw = snap.calls[0]
    assert "ORDER BY id" in sql and "LIMIT 3" in sql
    assert kw["timeout"] <= 3


@pytest.mark.parametrize("rows,limit,bytes_", [([(1, 2), (2, 3)], 1, 1000), ([(1, "x" * 100)], 5, 20)])
def test_spanner_limits(rows, limit, bytes_):
    snap = Snapshot(rows)
    admin = SpannerAdmin({}, database=SimpleNamespace(snapshot=lambda: snap))
    with pytest.raises(canon.CanonError, match="canonical-limit-exceeded"):
        exactdata.read_spanner(admin, "rows", None, limit, bytes_, 3)
    assert snap.closed


def test_kafka_projection_keeps_absent_null_and_types():
    rows = exactdata.project_records([{"value": {"id": 7, "nil": None}}],
                                     ["value.id", "value.nil", "value.missing"])
    assert rows == [{"value.id": 7, "value.nil": None}]
    decl = canon.declaration({"columns": {"value.id": "integer"}}, db_route=False)
    assert canon.compare([{"value.id": "7", "value.nil": None}], rows, decl)["equal"]
    assert not canon.compare([{"value.id": "7", "value.nil": None}], rows * 2, decl)["equal"]
    with pytest.raises(canon.CanonError, match="invalid-value:actual:value.id"):
        canon.compare([{"value.id": "7"}], [{"value.id": "7"}], decl)


@pytest.mark.parametrize("route", ["spanner-google", "spanner-postgres", "kafka"])
@pytest.mark.parametrize("broken", [False, True])
def test_route_assertion_records_typed_multiset(tmp_path, route, broken):
    from livetest import inputs
    from livetest.manifest import load_manifest
    import yaml
    (tmp_path / "app.tql").write_text("-- test app\n")
    (tmp_path / "expected").mkdir()
    kafka = route == "kafka"
    column = "value.id" if kafka else "id"
    (tmp_path / "expected/rows.csv").write_text(f"{column}\n1\n")
    spec = {"target": "rows", "db": route, "match": "expected/rows.csv",
            "exact": {"columns": {column: "integer"}}}
    if kafka:
        spec.update(project="kafka_record", keys=[column])
    raw = {"name": "typed-read", "tql": "app.tql", "requires": ["kafka" if kafka else "spanner"],
           "exact": {"version": 1}, "assert": {"data": [spec]}}
    path = tmp_path / "test.yaml"
    path.write_text(yaml.safe_dump(raw))
    m = load_manifest(path)
    m.timeout = 0
    if kafka:
        admin = SimpleNamespace(select_records=lambda *a, **kw: [{"value": {"id": 1}}] * (2 if broken else 1))
    else:
        snap = Snapshot([("1" if broken else 1, Decimal("2.50"))])
        # Project only the id: the typed reader must still preserve its token type.
        spec["keys"] = [column]
        raw["assert"]["data"] = [spec]
        path.write_text(yaml.safe_dump(raw))
        m = load_manifest(path)
        m.timeout = 0
        admin = SpannerAdmin({}, database=SimpleNamespace(snapshot=lambda: snap))
    comparisons = []
    def run():
        return exactdata.assert_exact_data({route: {"admin": admin}},
            exactdata.partition(m.assert_["data"], m, "data")[1], m, tokens={},
            owned=lambda *a: False, lifecycle=False, inputs=inputs.snapshot(m, path), collector=comparisons)
    if broken:
        with pytest.raises(AssertionFailed):
            run()
    else:
        assert run()[0]["status"] == "passed"
    assert comparisons[0]["equal"] is (not broken)
    assert comparisons[0]["actual"]["owned"] is False


def test_spanner_bytes_use_the_installed_clients_native_decoder():
    from google.cloud.spanner_v1._helpers import _parse_value_pb
    from google.cloud.spanner_v1.types import Type, TypeCode
    from google.protobuf.struct_pb2 import Value
    field_type = Type(code=TypeCode.BYTES)
    native = _parse_value_pb(Value(string_value="3q2+7w=="), field_type, "payload")
    class Result:
        fields = [SimpleNamespace(name="payload", type_=field_type)]
        def __iter__(self):
            return iter([(native,), (None,), (b"",)])
    snap = Snapshot([])
    snap.execute_sql = lambda *a, **kw: Result()
    admin = SpannerAdmin({}, database=SimpleNamespace(snapshot=lambda: snap))
    assert exactdata.read_spanner(admin, "rows", None, 3, 1000, 2) == [
        {"payload": bytes.fromhex("deadbeef")}, {"payload": None}, {"payload": b""}]
