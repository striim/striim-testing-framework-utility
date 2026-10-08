import io
import json

import pytest
import fastavro

from livetest.kafkaadmin import (
    KafkaAdmin, _parse_dsv, _check_topic, _frame, _unframe, _SEED_SCHEMA,
)

# ---- pure helpers -----------------------------------------------------------

def test_parse_dsv_positional_and_skips_blanks():
    assert _parse_dsv("1,alpha\n2,bravo\n\n") == [
        {"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]

def test_check_topic_rejects_bad():
    assert _check_topic("slt_src") == "slt_src"
    with pytest.raises(ValueError):
        _check_topic("bad/topic")

def test_frame_unframe_roundtrip():
    payload = b"\x01\x02\x03payload"
    framed = _frame(payload, 42)
    # frame is [int32 len=payload+4][int32 id=42][payload]
    assert len(framed) == 8 + len(payload)
    sid, body = _unframe(framed)
    assert sid == 42 and body == payload

def test_unframe_rejects_short():
    with pytest.raises(ValueError):
        _unframe(b"\x00\x00")

# ---- seed/read with injected fakes -----------------------------------------

class _FakeSR:
    def __init__(self, schema_str, sid=42):
        self._schema_str = schema_str; self._sid = sid; self.registered = []
    def register(self, subject, schema_str):
        self.registered.append((subject, schema_str)); return self._sid
    def get_schema_str(self, sid):
        assert sid == self._sid; return self._schema_str

class _FakeProducer:
    def __init__(self): self.produced = []
    def produce(self, topic, value=None, on_delivery=None):
        self.produced.append((topic, value))
        if on_delivery is not None:
            on_delivery(None, None)
    def flush(self, timeout=None): return 0

def _seed_admin():
    sr = _FakeSR(json.dumps(_SEED_SCHEMA))
    prod = _FakeProducer()
    admin = KafkaAdmin({"seed_topic": "slt_src", "broker": "x", "registry_url": "y"},
                       producer=prod, sr=sr)
    return admin, sr, prod

def test_run_sql_produces_framed_avro_to_source_topic():
    admin, sr, prod = _seed_admin()
    admin.run_sql("1,alpha\n2,bravo\n")
    assert sr.registered and sr.registered[0][0] == "slt_src-value"
    assert [t for t, _ in prod.produced] == ["slt_src", "slt_src"]
    # each produced value is a decodable Striim frame carrying the seeded row
    decoded = [r for _, v in prod.produced for r in admin._decode(v)]
    assert decoded == [{"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]

# ---- select_rows with a fake consumer --------------------------------------

class _FakeMsg:
    def __init__(self, value, key=None): self._v = value; self._k = key
    def value(self): return self._v
    def key(self): return self._k
    def error(self): return None

class _FakePart:
    def __init__(self, ids): self.partitions = {i: None for i in ids}; self.error = None

class _FakeConsumer:
    def __init__(self, topic, frames):
        self._topic = topic; self._frames = list(frames)
    def list_topics(self, topic, timeout=None):
        return type("MD", (), {"topics": {self._topic: _FakePart([0])}})()
    def assign(self, tps): pass
    def get_watermark_offsets(self, tp, timeout=None, cached=False): return (0, len(self._frames))
    def poll(self, t): return _FakeMsg(self._frames.pop(0)) if self._frames else None
    def close(self): pass

def _mk_frame(c0, c1, sid=42):
    parsed = fastavro.parse_schema(_SEED_SCHEMA)
    buf = io.BytesIO(); fastavro.schemaless_writer(buf, parsed, {"c0": c0, "c1": c1})
    return _frame(buf.getvalue(), sid)

def test_select_rows_consumes_and_decodes_all():
    sr = _FakeSR(json.dumps(_SEED_SCHEMA))
    frames = [_mk_frame("1", "alpha"), _mk_frame("2", "bravo")]
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "slt_src"},
                       sr=sr, consumer_factory=lambda g: _FakeConsumer("slt_tgt", frames))
    rows = admin.select_rows("slt_tgt")
    assert rows == [{"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]
    assert admin.count_rows("slt_tgt") == 2   # fresh consumer re-reads from offset 0


def test_select_records_preserves_key_value_association_and_typed_envelopes():
    schema = {"type": "record", "name": "Envelope", "fields": [
        {"name": "data", "type": {"type": "record", "name": "Row", "fields": [
            {"name": "ID", "type": "long"}, {"name": "RATE", "type": "double"}]}}]}
    parsed = fastavro.parse_schema(schema)
    def frame(number):
        buf = io.BytesIO()
        fastavro.schemaless_writer(buf, parsed, {"data": {"ID": number, "RATE": 3.25}})
        return _frame(buf.getvalue(), 42)
    messages = [_FakeMsg(frame(1) + frame(2), b'{"ID":"1"}'),
                _FakeMsg(frame(3), b'{"ID":"3"}')]
    closed = []
    class Consumer(_FakeConsumer):
        def poll(self, timeout):
            return self._frames.pop(0) if self._frames else None
        def close(self):
            closed.append(True)
    admin = KafkaAdmin({"broker": "x"}, sr=_FakeSR(json.dumps(schema)),
                       consumer_factory=lambda group: Consumer("slt_tgt", messages))
    assert admin.select_records("slt_tgt") == [
        {"key": {"ID": "1"}, "value": {"data": {"ID": 1, "RATE": 3.25}}},
        {"key": {"ID": "1"}, "value": {"data": {"ID": 2, "RATE": 3.25}}},
        {"key": {"ID": "3"}, "value": {"data": {"ID": 3, "RATE": 3.25}}}]
    assert closed == [True]


@pytest.mark.parametrize("key, expected", [
    (None, None), (b'null', None), (b'"null"', "null"),
    (b'{"ID":null}', {"ID": None}), (b'{"ID":"null"}', {"ID": "null"})])
def test_record_key_null_representations(key, expected):
    admin = KafkaAdmin({}, sr=_FakeSR(json.dumps(_SEED_SCHEMA)))
    assert admin._decode_record(_FakeMsg(_mk_frame("1", "alpha"), key))[0]["key"] == expected


@pytest.mark.parametrize("key", [b'', b'not-json', b'\xff', b'\x00\x00\x00\x01'])
def test_record_view_refuses_unsupported_key_encoding(key):
    admin = KafkaAdmin({}, sr=_FakeSR(json.dumps(_SEED_SCHEMA)))
    with pytest.raises(ValueError, match="JSON message key"):
        admin._decode_record(_FakeMsg(_mk_frame("1", "alpha"), key))


@pytest.mark.parametrize("extra", [None, b'', b'\x00', b'\x00\x00\x00\x02\x00\x00\x00\x2a',
                                  b'\x00\x00\x00\x40\x00\x00\x00\x2a'])
def test_record_view_refuses_silent_extra_messages(extra):
    # A matching four-row prefix must not hide a fifth tombstone or malformed value.
    frames = [_mk_frame(str(i), "row") for i in range(4)] + [extra]
    admin = KafkaAdmin({}, sr=_FakeSR(json.dumps(_SEED_SCHEMA)),
                       consumer_factory=lambda group: _FakeConsumer("slt_tgt", frames))
    assert len(admin.select_rows("slt_tgt")) == 4   # legacy value-only behavior is unchanged
    with pytest.raises(ValueError, match="kafka_record"):
        admin.select_records("slt_tgt")


def test_record_view_refuses_unread_bytes_after_or_inside_a_frame():
    admin = KafkaAdmin({}, sr=_FakeSR(json.dumps(_SEED_SCHEMA)))
    valid = _mk_frame("1", "alpha")
    for value in [valid+b'\x00', _frame(valid[8:]+b'\x00', 42)]:
        with pytest.raises(ValueError, match="Avro"):
            admin._decode_record(_FakeMsg(value))


def test_record_view_refuses_matching_prefix_before_captured_high_watermark(monkeypatch):
    closed = []
    class Consumer(_FakeConsumer):
        def close(self): closed.append(True)
    frames = [_mk_frame(str(i), "row") for i in range(5)]
    admin = KafkaAdmin({}, sr=_FakeSR(json.dumps(_SEED_SCHEMA)),
                       consumer_factory=lambda group: Consumer("slt_tgt", frames))
    # Consumer ID, deadline, four successful polls, then budget exhaustion.
    clock_values = [0, 0, 0.1, 0.2, 0.3, 0.4, 2]
    clock = iter(clock_values)
    monkeypatch.setattr("livetest.kafkaadmin.time.monotonic", lambda: next(clock))
    assert len(admin.select_rows("slt_tgt", poll_budget=1)) == 4
    clock = iter(clock_values)
    with pytest.raises(RuntimeError, match="1 captured message.*unread"):
        admin.select_records("slt_tgt", poll_budget=1)
    assert closed == [True, True]

def test_decode_multiframe_and_strips_striim_metadata():
    # Striim packs several records into ONE message as consecutive frames, and
    # AvroFormatter adds a __striimmetadata field — decode must split all frames and
    # drop __-prefixed keys.
    schema = {"type": "record", "name": "R", "namespace": "slt", "fields": [
        {"name": "c0", "type": "string"}, {"name": "c1", "type": "string"},
        {"name": "__striimmetadata", "type": ["null", "string"], "default": None}]}
    sr = _FakeSR(json.dumps(schema))
    parsed = fastavro.parse_schema(schema)
    def mk(c0, c1):
        b = io.BytesIO()
        fastavro.schemaless_writer(b, parsed, {"c0": c0, "c1": c1, "__striimmetadata": "meta"})
        return _frame(b.getvalue(), 42)
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "s"}, sr=sr)
    one_message = mk("1", "alpha") + mk("2", "bravo")   # two frames, one message value
    assert admin._decode(one_message) == [{"c0": "1", "c1": "alpha"}, {"c0": "2", "c1": "bravo"}]

def test_select_rows_empty_when_topic_absent():
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "slt_src"},
                       consumer_factory=lambda g: _FakeConsumer("other", []))
    assert admin.select_rows("missing") == []

# ---- clear_topic delete-completion (fake admin client) ----------------------

class _FakeFuture:
    def result(self, timeout=None): pass

class _FakeMD:
    def __init__(self, names): self.topics = {n: None for n in names}

class _StuckAdmin:
    # Reports the topic present forever; delete_topics "succeeds" but never removes it
    # (models a delete that didn't propagate within the poll window).
    def __init__(self, names): self._names = set(names)
    def list_topics(self, timeout=None): return _FakeMD(self._names)
    def delete_topics(self, topics, operation_timeout=None):
        return {t: _FakeFuture() for t in topics}   # no removal

class _DeletingAdmin:
    # Delete actually removes the topic; create_topics re-adds it.
    def __init__(self, names): self._names = set(names)
    def list_topics(self, timeout=None): return _FakeMD(self._names)
    def delete_topics(self, topics, operation_timeout=None):
        for t in topics: self._names.discard(t)
        return {t: _FakeFuture() for t in topics}
    def create_topics(self, newtopics):
        for nt in newtopics: self._names.add(nt.topic)
        return {nt.topic: _FakeFuture() for nt in newtopics}

class _FakeSubjSR:
    def __init__(self): self.deleted = []
    def delete_subject(self, subject): self.deleted.append(subject)

def test_clear_topic_raises_when_delete_does_not_complete(monkeypatch):
    monkeypatch.setattr("livetest.kafkaadmin.time.sleep", lambda s: None)   # no real waiting
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "s"},
                       admin=_StuckAdmin({"slt_tgt"}), sr=_FakeSubjSR())
    # The old code recreated over the still-present topic -> a prior run's messages
    # survive (false pass). Now it must raise instead.
    with pytest.raises(RuntimeError, match="still present"):
        admin.clear_topic("slt_tgt")

def test_clear_topic_recreates_after_successful_delete(monkeypatch):
    monkeypatch.setattr("livetest.kafkaadmin.time.sleep", lambda s: None)
    fake = _DeletingAdmin({"slt_tgt"})
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "s"},
                       admin=fake, sr=_FakeSubjSR())
    admin.clear_topic("slt_tgt")                       # delete completes -> recreate, no raise
    assert "slt_tgt" in fake._names                    # topic exists again (empty)

def test_registry_delete_subject_hard_deletes(monkeypatch):
    # Regression: delete_subject must actually issue the HTTP deletes. It once referenced
    # `requests` without importing it in-method -> NameError, swallowed by the best-effort
    # try/except -> the subject was NEVER cleared -> a re-register hit 409 Conflict. And a
    # soft delete alone isn't enough (a re-register still 409s), so it must go permanent too.
    import requests
    from livetest.kafkaadmin import _Registry
    calls = []
    monkeypatch.setattr(requests, "delete",
                        lambda url, params=None, timeout=None: calls.append((url, params)))
    _Registry("http://sr:8081/").delete_subject("slt_src-value")
    assert calls == [
        ("http://sr:8081/subjects/slt_src-value", None),
        ("http://sr:8081/subjects/slt_src-value", {"permanent": "true"}),
    ]


def test_delete_topic_removes_topic_and_subject(monkeypatch):
    monkeypatch.setattr("livetest.kafkaadmin.time.sleep", lambda s: None)
    fake = _DeletingAdmin({"slt_t1_src"})
    sr = _FakeSubjSR()
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "s"},
                       admin=fake, sr=sr)
    admin.delete_topic("slt_t1_src")
    assert "slt_t1_src" not in fake._names   # gone, NOT recreated (unlike clear_topic)
    assert sr.deleted == ["slt_t1_src-value"]


def test_delete_topic_is_a_noop_when_absent(monkeypatch):
    monkeypatch.setattr("livetest.kafkaadmin.time.sleep", lambda s: None)
    fake = _DeletingAdmin(set())
    admin = KafkaAdmin({"broker": "x", "registry_url": "y", "seed_topic": "s"},
                       admin=fake, sr=_FakeSubjSR())
    admin.delete_topic("slt_missing_src")   # must not raise
