"""KafkaAdmin's requests and refusals against fake clients (FW-115): the registry REST calls,
the Debezium seed, the audit-topic and topic-inventory checks, and the read loop's refusals."""
import io
import json

import fastavro
import pytest
import requests

from livetest import kafkaadmin
from livetest.canon import CanonError
from livetest.kafkaadmin import (
    KafkaAdmin, _Registry, _SEED_SCHEMA, _debezium_envelope_schema, _frame, _iter_frames,
)


# ---- _Registry._req ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, body=None, error=None):
        self._body, self._error = body, error
    def raise_for_status(self):
        if self._error is not None:
            raise self._error
    def json(self):
        return self._body


@pytest.fixture
def http(monkeypatch):
    """requests.request replaced by a script of responses/exceptions; sleeps recorded, not slept."""
    state = {"calls": [], "script": [], "sleeps": []}
    def request(method, url, **kw):
        state["calls"].append((method, url, kw))
        step = state["script"].pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(kafkaadmin.time, "sleep", lambda s: state["sleeps"].append(s))
    return state


def test_register_posts_the_avro_schema_to_the_subject_and_returns_its_id(http):
    http["script"] = [_Resp({"id": "17"})]
    sid = _Registry("http://sr:8081/").register("slt_src-value", '{"type":"string"}')
    assert sid == 17 and isinstance(sid, int)
    assert http["calls"] == [("POST", "http://sr:8081/subjects/slt_src-value/versions", {
        "timeout": 15,
        "headers": {"Content-Type": "application/vnd.schemaregistry.v1+json"},
        "json": {"schema": '{"type":"string"}', "schemaType": "AVRO"}})]


def test_get_schema_str_fetches_the_schema_by_id(http):
    http["script"] = [_Resp({"schema": '{"type":"int"}'})]
    assert _Registry("http://sr:8081").get_schema_str(42) == '{"type":"int"}'
    assert http["calls"] == [("GET", "http://sr:8081/schemas/ids/42", {"timeout": 15})]


def test_req_retries_connection_errors_then_returns_the_body(http):
    http["script"] = [requests.exceptions.ConnectionError("refused"),
                      requests.exceptions.ConnectionError("reset"), _Resp({"ok": 1})]
    assert _Registry("http://sr")._req("GET", "/x", attempts=5, delay=0.25) == {"ok": 1}
    assert len(http["calls"]) == 3 and http["sleeps"] == [0.25, 0.25]


def test_req_raises_the_last_connection_error_after_all_attempts(http):
    errors = [requests.exceptions.ConnectionError(f"down {i}") for i in range(3)]
    http["script"] = list(errors)
    with pytest.raises(requests.exceptions.ConnectionError) as info:
        _Registry("http://sr")._req("GET", "/x", attempts=3, delay=0)
    assert info.value is errors[-1] and len(http["calls"]) == 3


def test_req_does_not_retry_an_http_error(http):
    http["script"] = [_Resp(error=requests.exceptions.HTTPError("409 Conflict")), _Resp({"id": 1})]
    with pytest.raises(requests.exceptions.HTTPError, match="409"):
        _Registry("http://sr").register("s-value", "{}")
    assert len(http["calls"]) == 1 and http["sleeps"] == []


# ---- seeding: fakes ----------------------------------------------------------------------------

class _SR:
    """A registry that hands out a fresh id per register call and serves what it registered."""
    def __init__(self):
        self.registered, self._by_id = [], {}
    def register(self, subject, schema_str):
        sid = 100 + len(self.registered)
        self.registered.append((subject, schema_str))
        self._by_id[sid] = schema_str
        return sid
    def get_schema_str(self, sid):
        return self._by_id[sid]


class _Producer:
    def __init__(self, pending=0, fail=None):
        self.produced, self.flushes = [], []
        self._pending, self._fail = pending, fail
    def produce(self, topic, value=None, on_delivery=None):
        self.produced.append((topic, value))
        on_delivery(self._fail, None)
    def flush(self, timeout=None):
        self.flushes.append(timeout)
        return self._pending


def _admin(prod=None, sr=None, **kw):
    return KafkaAdmin({"seed_topic": "slt_src", "broker": "b", "registry_url": "r"},
                      producer=prod or _Producer(), sr=sr or _SR(), **kw)


def _decode_envelopes(sr, produced):
    out = []
    for _topic, value in produced:
        for sid, payload in _iter_frames(value, strict=True):
            parsed = fastavro.parse_schema(json.loads(sr.get_schema_str(sid)))
            out.append((sid, fastavro.schemaless_reader(io.BytesIO(payload), parsed)))
    return out


_EVENTS = [
    {"op": "c", "source": {"db": "d", "table": "t"}, "fieldTypes": {"id": "int", "name": "string"},
     "after": {"id": 1, "name": "one"}},
    {"op": "u", "source": {"db": "d", "table": "t"}, "fieldTypes": {"id": "int", "name": "string"},
     "before": {"id": 1, "name": "one"}, "after": {"id": 1, "name": "uno"}},
    {"op": "c", "enabled": "False", "source": {"db": "d", "table": "t"},
     "fieldTypes": {"id": "int"}, "after": {"id": 9}},
    {"op": "d", "source": {"db": "d", "table": "u"}, "fieldTypes": {"k": "long"},
     "before": {"k": 5}},
]


# ---- seeding: Debezium envelopes ---------------------------------------------------------------

def test_a_json_array_seeds_debezium_envelopes_registered_once_per_schema():
    prod, sr = _Producer(), _SR()
    _admin(prod, sr).run_sql("  \n" + json.dumps(_EVENTS))
    # two distinct schemas -> two registrations under the seed topic's subject, in event order
    assert sr.registered == [
        ("slt_src-value", json.dumps(_debezium_envelope_schema(
            {"id": "int", "name": "string"}, {"db": "d", "table": "t"}))),
        ("slt_src-value", json.dumps(_debezium_envelope_schema({"k": "long"}, {"db": "d", "table": "u"}))),
    ]
    # the disabled event is skipped; each other event is one frame on the seed topic, carrying its schema id
    assert [t for t, _ in prod.produced] == ["slt_src"] * 3
    got = _decode_envelopes(sr, prod.produced)
    assert [sid for sid, _ in got] == [100, 100, 101]
    assert [r["op"] for _, r in got] == ["c", "u", "d"]
    assert got[0][1]["after"] == {"id": 1, "name": "one"} and got[0][1]["before"] is None
    assert got[1][1]["before"] == {"id": 1, "name": "one"} and got[1][1]["after"] == {"id": 1, "name": "uno"}
    assert got[2][1]["before"] == {"k": 5} and got[2][1]["after"] is None
    assert got[2][1]["source"] == {"db": "d", "table": "u"}
    assert prod.flushes == [30]


def test_an_empty_json_array_seeds_nothing_and_still_flushes():
    prod, sr = _Producer(), _SR()
    _admin(prod, sr).run_sql("[]")
    assert sr.registered == [] and prod.produced == [] and prod.flushes == [30]


def test_a_bracketed_line_that_is_not_json_seeds_as_csv():
    prod, sr = _Producer(), _SR()
    admin = _admin(prod, sr)
    admin.run_sql("[a,b\n2,c\n")
    assert sr.registered == [("slt_src-value", json.dumps(_SEED_SCHEMA))]
    rows = [r for _, v in prod.produced for r in admin._decode(v)]
    assert rows == [{"c0": "[a", "c1": "b"}, {"c0": "2", "c1": "c"}]


@pytest.mark.parametrize("content", ["1,a\n", json.dumps(_EVENTS[:1])], ids=["csv", "debezium"])
def test_seed_refuses_undelivered_messages_after_the_flush(content):
    with pytest.raises(RuntimeError, match=r"1 message\(s\) to topic 'slt_src' still undelivered"):
        _admin(_Producer(pending=1)).run_sql(content)


@pytest.mark.parametrize("content", ["1,a\n2,b\n", json.dumps(_EVENTS[:2])], ids=["csv", "debezium"])
def test_seed_refuses_a_failed_delivery(content):
    with pytest.raises(RuntimeError, match=r"delivery failed for 2 message\(s\) to topic 'slt_src': broker said no"):
        _admin(_Producer(fail="broker said no")).run_sql(content)


# ---- check_audit_topic --------------------------------------------------------------------------

class _Entry:
    def __init__(self, value): self.value = value


class _Future:
    def __init__(self, value): self._value, self.timeouts = value, []
    def result(self, timeout=None):
        self.timeouts.append(timeout)
        return self._value


class _ConfigAdmin:
    def __init__(self, configs):
        self.configs, self.requests, self.future = configs, [], None
    def describe_configs(self, resources):
        self.requests.append(list(resources))
        self.future = _Future({k: _Entry(v) for k, v in self.configs.items()})
        return {resources[0]: self.future}


_APPEND_ONLY = {"cleanup.policy": "delete", "retention.ms": "-1", "retention.bytes": "-1"}


def test_check_audit_topic_describes_the_topic_and_accepts_an_append_only_one():
    from confluent_kafka.admin import ResourceType
    fake = _ConfigAdmin(_APPEND_ONLY)
    assert _admin(admin=fake).check_audit_topic("slt_audit") is None
    [[resource]] = fake.requests
    assert resource.restype == ResourceType.TOPIC and resource.name == "slt_audit"
    assert fake.future.timeouts == [10]


@pytest.mark.parametrize("key,value", [("cleanup.policy", "compact"), ("cleanup.policy", "compact,delete"),
                                       ("retention.ms", "604800000"), ("retention.bytes", "1073741824")])
def test_check_audit_topic_refuses_a_topic_that_can_lose_records(key, value):
    with pytest.raises(ValueError, match="audit-target-not-append-only"):
        _admin(admin=_ConfigAdmin({**_APPEND_ONLY, key: value})).check_audit_topic("slt_audit")


def test_check_audit_topic_refuses_an_unsafe_name_before_asking_the_broker():
    fake = _ConfigAdmin(_APPEND_ONLY)
    with pytest.raises(ValueError, match="unsafe topic name"):
        _admin(admin=fake).check_audit_topic("../audit")
    assert fake.requests == []


# ---- topic_exists ------------------------------------------------------------------------------

class _Topic:
    def __init__(self, error=None): self.error = error


class _InventoryAdmin:
    def __init__(self, topics): self.topics, self.timeouts = topics, []
    def list_topics(self, timeout=None):
        self.timeouts.append(timeout)
        return type("MD", (), {"topics": self.topics})()


def test_topic_exists_reads_the_broker_inventory():
    fake = _InventoryAdmin({"present": _Topic()})
    admin = _admin(admin=fake)
    assert admin.topic_exists("present") is True
    assert admin.topic_exists("absent") is False
    assert fake.timeouts == [10, 10]


def test_topic_exists_refuses_an_unreadable_inventory_entry():
    with pytest.raises(RuntimeError, match="Kafka topic inventory unreadable: broken"):
        _admin(admin=_InventoryAdmin({"broken": _Topic(error="LEADER_NOT_AVAILABLE")})).topic_exists("broken")


def test_topic_exists_refuses_an_unsafe_name():
    with pytest.raises(ValueError, match="unsafe topic name"):
        _admin(admin=_InventoryAdmin({})).topic_exists("a b")


# ---- _select ------------------------------------------------------------------------------------

def _seed_frame(c0, c1, sid=100):
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(_SEED_SCHEMA), {"c0": c0, "c1": c1})
    return _frame(buf.getvalue(), sid)


class _Msg:
    def __init__(self, partition, offset, value, key=None):
        self._p, self._o, self._v, self._k = partition, offset, value, key
    def error(self): return None
    def partition(self): return self._p
    def offset(self): return self._o
    def value(self): return self._v
    def key(self): return self._k


class _Consumer:
    def __init__(self, topic, partitions, watermarks, messages):
        self.topic, self.partitions, self.watermarks = topic, partitions, watermarks
        self.messages, self.assigned, self.closed = list(messages), None, False
    def list_topics(self, topic, timeout=None):
        return type("MD", (), {"topics": {self.topic: type("T", (), {
            "partitions": {p: None for p in self.partitions}, "error": None})()}})()
    def get_watermark_offsets(self, tp, timeout=None, cached=False):
        return self.watermarks[tp.partition]
    def assign(self, tps):
        self.assigned = [(tp.topic, tp.partition, tp.offset) for tp in tps]
    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None
    def close(self):
        self.closed = True


def _reader(consumer, groups=None):
    sr = _SR()
    sr._by_id[100] = json.dumps(_SEED_SCHEMA)
    def factory(group):
        if groups is not None:
            groups.append(group)
        return consumer
    return KafkaAdmin({}, sr=sr, consumer_factory=factory)


def test_select_assigns_each_partition_at_its_low_watermark_and_skips_out_of_range_offsets():
    groups = []
    c = _Consumer("t", [0], {0: (3, 5)}, [
        _Msg(0, 2, _seed_frame("old", "x")),       # below the low watermark: not read
        _Msg(0, 3, _seed_frame("a", "x")),
        _Msg(0, 3, _seed_frame("dup", "x")),       # redelivered offset: not read twice
        _Msg(0, 4, _seed_frame("b", "x"))])
    assert _reader(c, groups).select_rows("t") == [{"c0": "a", "c1": "x"}, {"c0": "b", "c1": "x"}]
    assert c.assigned == [("t", 0, 3)] and c.closed
    assert len(groups) == 1 and groups[0].startswith("slt-read-t-")


def test_a_non_strict_read_ignores_a_partition_it_did_not_capture():
    c = _Consumer("t", [0], {0: (0, 1)}, [_Msg(7, 0, _seed_frame("stray", "x")),
                                          _Msg(0, 0, _seed_frame("a", "x"))])
    assert _reader(c).select_rows("t") == [{"c0": "a", "c1": "x"}]


def test_a_strict_read_refuses_a_partition_it_did_not_capture():
    c = _Consumer("t", [0], {0: (0, 1)}, [_Msg(7, 0, _seed_frame("stray", "x"), key=b"1")])
    with pytest.raises(RuntimeError, match="uncaptured partition"):
        _reader(c).select_records("t", strict=True)
    assert c.closed


def test_a_strict_read_refuses_once_its_deadline_passes(monkeypatch):
    now = [0.0]
    def monotonic():
        now[0] += 1.0                                   # every clock read costs one second
        return now[0]
    monkeypatch.setattr(kafkaadmin.time, "monotonic", monotonic)
    c = _Consumer("t", [0], {0: (0, 1)}, [])            # the one captured offset never arrives
    with pytest.raises(RuntimeError, match="kafka_record read deadline exceeded"):
        _reader(c).select_records("t", poll_budget=4, strict=True)
    assert c.closed


def _two_keyed_messages():
    value = _seed_frame("a", "x")
    return value, [_Msg(0, 0, value, key=b"1"), _Msg(0, 1, value, key=b"2")]


def test_a_read_refuses_more_wire_bytes_than_max_bytes_counting_keys_and_values():
    value, messages = _two_keyed_messages()
    c = _Consumer("t", [0], {0: (0, 2)}, messages)
    with pytest.raises(CanonError) as info:
        _reader(c).select_records("t", max_bytes=2 * len(value) + 1)   # two 1-byte keys make it +2
    assert info.value.code == "canonical-limit-exceeded" and "max_bytes" in str(info.value)


def test_a_read_at_exactly_max_bytes_is_accepted():
    value, messages = _two_keyed_messages()
    c = _Consumer("t", [0], {0: (0, 2)}, messages)
    assert len(_reader(c).select_records("t", max_bytes=2 * len(value) + 2)) == 2


def test_a_read_refuses_more_decoded_rows_than_max_rows():
    two = _seed_frame("a", "x") + _seed_frame("b", "x")
    c = _Consumer("t", [0], {0: (0, 1)}, [_Msg(0, 0, two, key=b"1")])
    with pytest.raises(CanonError) as info:
        _reader(c).select_records("t", max_rows=1)
    assert info.value.code == "canonical-limit-exceeded" and "max_rows" in str(info.value)
    c = _Consumer("t", [0], {0: (0, 1)}, [_Msg(0, 0, two, key=b"1")])
    assert len(_reader(c).select_records("t", max_rows=2)) == 2


def test_a_json_payload_read_keeps_keys_and_refuses_invalid_json():
    c = _Consumer("t", [0], {0: (0, 2)}, [_Msg(0, 0, b'{"v": 1}', key=b'{"k": 1}'),
                                          _Msg(0, 1, b'{"v": 2}', key=None)])
    assert _reader(c).select_records("t", payload="json") == [
        {"key": {"k": 1}, "value": {"v": 1}}, {"key": None, "value": {"v": 2}}]
    c = _Consumer("t", [0], {0: (0, 1)}, [_Msg(0, 0, b"not json", key=None)])
    with pytest.raises(CanonError) as info:
        _reader(c).select_records("t", payload="json")
    assert info.value.code == "invalid-json"
