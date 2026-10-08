from __future__ import annotations
import csv
import io
import json
import re
import struct
import time

# Kafka admin — the streaming counterpart to PgAdmin/GcsAdmin/SpannerAdmin, for a
# KafkaReader source + KafkaWriter target with Avro via a Confluent Schema Registry.
# Ensures/clears topics, seeds Avro messages into the source topic, and reads a topic
# back for the diff.
#
# WIRE FORMAT: Striim's Avro+registry path does NOT use the Confluent standard
# (magic byte 0x00 + int32 schemaId + payload). It is length-delimited, big-endian:
#
#     [int32 length][int32 schemaId][avro payload]      (length = len(payload) + 4)
#
# read by the KafkaReader's com.striim.avro.deserializer.LengthDelimitedAvroRecordDeserializer
# and written by the KafkaWriter's AvroFormatter. We match it on BOTH ends: seed
# produces frames the reader can consume, and select_rows decodes the frames the
# writer produced. (So confluent-kafka's stock Avro (de)serializers are unusable here
# — we frame by hand and use fastavro for the payload.)

_TOPIC = re.compile(r"^[A-Za-z0-9._-]+$")

# The seed schema: two string columns, matching the kafka-diff CQ projection (c0,c1).
_SEED_SCHEMA = {
    "type": "record", "name": "SltRecord", "namespace": "slt",
    "fields": [{"name": "c0", "type": "string"}, {"name": "c1", "type": "string"}],
}

# Debezium-envelope Avro (for AvroConverterOp), mirroring the operator's DebeziumEventBuilder.
_AVRO_T = {"int": "int", "long": "long", "float": "float", "double": "double",
           "boolean": "boolean", "string": "string"}

def _debezium_envelope_schema(field_types: dict, source: dict) -> dict:
    # Value (optional, typed per fieldTypes) + Source (optional strings) + the Envelope
    # {op, source, before, after}. `after` references test.Value by name -- Avro forbids
    # redefining the record `before` already inlined.
    value_s = {"type": "record", "name": "Value", "namespace": "test",
               "fields": [{"name": k, "type": ["null", _AVRO_T.get(v, "string")], "default": None}
                          for k, v in field_types.items()]}
    source_s = {"type": "record", "name": "Source", "namespace": "test",
                "fields": [{"name": k, "type": ["null", "string"], "default": None} for k in source]}
    return {"type": "record", "name": "Envelope", "namespace": "test", "fields": [
        {"name": "op", "type": "string"},
        {"name": "source", "type": ["null", source_s], "default": None},
        {"name": "before", "type": ["null", value_s], "default": None},
        {"name": "after", "type": ["null", "test.Value"], "default": None},
    ]}

def _check_topic(topic: str) -> str:
    if not _TOPIC.match(topic or ""):
        raise ValueError(f"unsafe topic name: {topic!r}")
    return topic

def _parse_dsv(text: str) -> list[dict]:
    # DSV == CSV (comma-delimited). Positional columns -> {c0, c1, ...}; skip blanks.
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if rec and any(f.strip() for f in rec):
            rows.append({f"c{i}": v for i, v in enumerate(rec)})
    return rows

def _frame(payload: bytes, schema_id: int) -> bytes:
    # Striim length-delimited frame: [int32 len][int32 schemaId][payload], big-endian.
    return struct.pack(">i", len(payload) + 4) + struct.pack(">i", schema_id) + payload

def _unframe(value: bytes) -> tuple[int, bytes]:
    # -> (schemaId, avro payload) of the FIRST frame. See _iter_frames for the general
    # case: Striim packs MANY frames into one Kafka message value.
    if value is None or len(value) < 8:
        raise ValueError("message too short to be a length-delimited Avro frame")
    length = struct.unpack(">i", value[0:4])[0]
    schema_id = struct.unpack(">i", value[4:8])[0]
    return schema_id, value[8:4 + length]

def _iter_frames(value: bytes, *, strict: bool = False):
    # Yield (schemaId, avro payload) for EVERY frame in a message value. Striim's
    # KafkaWriter batches multiple records into a single Kafka message as consecutive
    # frames: [int32 length][int32 schemaId][payload], where length = 4 + len(payload),
    # so the next frame starts 4 + length bytes on. (Reading only the first frame is why
    # a 3-record batch looked like 1 row.)
    pos, n = 0, len(value or b"")
    if strict and n == 0:
        raise ValueError("kafka_record does not support null or empty message values")
    while pos + 8 <= n:
        length = struct.unpack(">i", value[pos:pos + 4])[0]
        if length < 4 or pos + 4 + length > n:
            if strict:
                raise ValueError(f"kafka_record has a malformed Avro frame at byte {pos}")
            break
        schema_id = struct.unpack(">i", value[pos + 4:pos + 8])[0]
        yield schema_id, value[pos + 8:pos + 4 + length]
        pos += 4 + length
    if strict and pos != n:
        raise ValueError(f"kafka_record has trailing or truncated Avro bytes at byte {pos}")

class _Registry:
    # Minimal Confluent Schema Registry REST client (on `requests`, already a dep —
    # confluent-kafka's own client pulls httpx/authlib we don't want). Only the two
    # calls we need: register a schema under a subject, and fetch a schema by id.
    _CT = "application/vnd.schemaregistry.v1+json"

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _req(self, method: str, path: str, attempts: int = 12, delay: float = 1.5, **kw):
        # Retry connection-level failures: on a cold start the registry JVM may still be
        # coming up (a reused live registry has no healthcheck to gate on), so the first
        # call can get a connection reset/refused before it serves. HTTP errors (4xx/5xx)
        # are not retried — those are real.
        import requests
        last = None
        for _ in range(attempts):
            try:
                r = requests.request(method, f"{self.url}{path}", timeout=15, **kw)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.ConnectionError as e:
                last = e
                time.sleep(delay)
        raise last

    def register(self, subject: str, schema_str: str) -> int:
        body = self._req("POST", f"/subjects/{subject}/versions",
                         headers={"Content-Type": self._CT},
                         json={"schema": schema_str, "schemaType": "AVRO"})
        return int(body["id"])

    def get_schema_str(self, schema_id: int) -> str:
        return self._req("GET", f"/schemas/ids/{schema_id}")["schema"]

    def delete_subject(self, subject: str) -> None:
        # HARD delete: soft-delete then permanent. A soft delete alone leaves the subject in a
        # state where re-registering the same schema returns 409 Conflict, so each run's seed
        # register must start from a truly clean subject. Schema Registry requires the soft
        # delete before the permanent one; a 404 at either step (never registered) is fine.
        import requests   # module imports `requests` locally per-method (see _req)
        for params in (None, {"permanent": "true"}):
            try:
                requests.delete(f"{self.url}/subjects/{subject}", params=params, timeout=10)
            except Exception:                          # noqa: BLE001 — best effort
                pass
        return


class KafkaAdmin:
    def __init__(self, dsn: dict, *, producer=None, admin=None, sr=None, consumer_factory=None):
        # dsn: {broker, registry_url, seed_topic}
        self.dsn = dsn
        self._producer = producer
        self._admin = admin
        self._sr = sr
        self._consumer_factory = consumer_factory
        self._parsed_cache: dict[int, dict] = {}   # schema_id -> fastavro parsed schema

    # ---- lazy clients (injectable for hermetic tests) -----------------------

    def _sr_client(self):
        if self._sr is None:
            self._sr = _Registry(self.dsn["registry_url"])
        return self._sr

    def _prod(self):
        if self._producer is None:
            from confluent_kafka import Producer
            self._producer = Producer({"bootstrap.servers": self.dsn["broker"]})
        return self._producer

    def _admin_client(self):
        if self._admin is None:
            from confluent_kafka.admin import AdminClient
            self._admin = AdminClient({"bootstrap.servers": self.dsn["broker"]})
        return self._admin

    def _consumer(self, group: str):
        if self._consumer_factory is not None:
            return self._consumer_factory(group)
        from confluent_kafka import Consumer
        return Consumer({"bootstrap.servers": self.dsn["broker"], "group.id": group,
                         "auto.offset.reset": "earliest", "enable.auto.commit": False})

    # ---- schema helpers -----------------------------------------------------

    def _parsed_schema(self, schema_id: int):
        import fastavro
        if schema_id not in self._parsed_cache:
            s_str = self._sr_client().get_schema_str(schema_id)
            self._parsed_cache[schema_id] = fastavro.parse_schema(json.loads(s_str))
        return self._parsed_cache[schema_id]

    def _decode(self, value: bytes) -> list[dict]:
        # One Kafka message value may carry several frames -> several rows. Normalize
        # values to strings (source seeded as CSV vs target re-encoded from a
        # string-typed stream) and drop Striim's __-prefixed metadata (e.g.
        # __striimmetadata) that AvroFormatter adds, so source and target compare equal.
        return [{k: (None if v is None else str(v))
                 for k, v in rec.items() if not k.startswith("__")}
                for rec in self._decode_values(value)]

    def _decode_values(self, value: bytes, *, strict: bool = False) -> list[dict]:
        import fastavro
        values = []
        for schema_id, payload in _iter_frames(value, strict=strict):
            stream = io.BytesIO(payload)
            decoded = fastavro.schemaless_reader(stream, self._parsed_schema(schema_id))
            if strict and stream.tell() != len(payload):
                raise ValueError("kafka_record has unread bytes inside an Avro payload")
            values.append(decoded)
        return values

    def _decode_record(self, message) -> list[dict]:
        """Keep each JSON message key attached to its typed Avro value frame.

        This opt-in view deliberately rejects non-JSON keys. A null Kafka key,
        JSON null, and the JSON string "null" retain their decoded meanings.
        """
        raw_key = message.key()
        try:
            key = None if raw_key is None else json.loads(raw_key)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("kafka_record requires a JSON message key or a null Kafka key") from error
        return [{"key": key, "value": value} for value in self._decode_values(message.value(), strict=True)]

    # ---- topic lifecycle ----------------------------------------------------

    def ensure_topic(self, topic: str, partitions: int = 1) -> None:
        from confluent_kafka.admin import NewTopic
        a = self._admin_client()
        if _check_topic(topic) in a.list_topics(timeout=10).topics:
            return
        for _t, f in a.create_topics([NewTopic(topic, num_partitions=partitions,
                                               replication_factor=1)]).items():
            try:
                f.result(30)
            except Exception as e:                        # noqa: BLE001
                if "already exists" not in str(e).lower():
                    raise
        for _ in range(30):
            if topic in a.list_topics(timeout=10).topics:
                return
            time.sleep(1)

    def clear_topic(self, topic: str) -> None:
        # Delete + recreate so each test starts empty: a prior run's messages (a
        # matching target especially) would otherwise satisfy the diff (false pass).
        a = self._admin_client()
        present = _check_topic(topic) in a.list_topics(timeout=10).topics
        deleted = True
        if present:
            for _t, f in a.delete_topics([topic], operation_timeout=30).items():
                try:
                    f.result(30)
                except Exception:                          # noqa: BLE001 — best effort
                    pass
            deleted = False
            for _ in range(30):
                if topic not in a.list_topics(timeout=10).topics:
                    deleted = True
                    break
                time.sleep(1)
        # Clear the topic's schema-registry subject FIRST, and ALWAYS — even if the topic
        # delete is still lagging (we may raise below). A left-behind subject makes the next
        # run's seed register hit 409 Conflict; and its version history would otherwise grow
        # unbounded across a long SLT_KEEP_SERVICES session. delete_subject is a hard delete.
        try:
            self._sr_client().delete_subject(f"{topic}-value")
        except Exception:                              # noqa: BLE001 — best effort
            pass
        if present and not deleted:
            # Deletion didn't complete in the poll window. Do NOT fall through to ensure_topic:
            # it would find the OLD topic still present and treat that as success
            # (recreate-is-a-no-op), leaving a prior run's messages in place -> a false PASS.
            raise RuntimeError(
                f"clear_topic: topic {topic!r} still present after delete window; "
                f"refusing to reuse it (a prior run's messages would survive)")
        self.ensure_topic(topic)

    def delete_topic(self, topic: str) -> None:
        # DELETE without recreate (unlike clear_topic, which deletes+recreates for
        # per-test setup) -- used at per-test TEARDOWN now that topics are per-test
        # (derive_per_test_base), so nothing else needs this topic again.
        a = self._admin_client()
        if _check_topic(topic) in a.list_topics(timeout=10).topics:
            for _t, f in a.delete_topics([topic], operation_timeout=30).items():
                try:
                    f.result(30)
                except Exception:                          # noqa: BLE001 — best effort
                    pass
        try:
            self._sr_client().delete_subject(f"{topic}-value")
        except Exception:                                  # noqa: BLE001 — best effort
            pass

    # ---- seed + read (the admin contract used by the diff tier) -------------

    def _flush_checked(self, prod, topic: str, errors: list) -> None:
        # confluent-kafka's produce() is async and flush()'s return value (messages still
        # queued after the timeout) is easy to ignore -- do that and a broker-side delivery
        # failure or a timeout leaves the seed topic silently short/empty: the seed step
        # "succeeds", the app deploys with nothing to read, and the data assertion just
        # times out 300s later with no clue why. Fail loudly here instead.
        pending = prod.flush(30)
        if pending:
            raise RuntimeError(
                f"Kafka seed: {pending} message(s) to topic {topic!r} still undelivered "
                f"after the 30s flush timeout")
        if errors:
            raise RuntimeError(
                f"Kafka seed: delivery failed for {len(errors)} message(s) to topic "
                f"{topic!r}: {errors[0]}")

    def run_sql(self, content: str) -> None:
        # Seed the source topic. A JSON array of Debezium events (op/source/after/before
        # + fieldTypes) -> Debezium-envelope Avro (for AvroConverterOp). Anything else ->
        # the flat CSV seed (c0,c1) the diff tier uses. Both produce Striim
        # length-delimited Avro frames the KafkaReader + AvroParser can consume.
        stripped = content.lstrip()
        if stripped.startswith("["):
            try:
                events = json.loads(content)
            except json.JSONDecodeError:
                events = None
            if isinstance(events, list):
                self._seed_debezium(events)
                return
        topic = self.dsn["seed_topic"]
        schema_id = self._sr_client().register(f"{topic}-value", json.dumps(_SEED_SCHEMA))
        parsed = self._parsed_schema(schema_id)
        prod = self._prod()
        errors: list = []
        def _on_delivery(err, _msg):
            if err is not None:
                errors.append(err)
        import fastavro
        for row in _parse_dsv(content):
            buf = io.BytesIO()
            fastavro.schemaless_writer(buf, parsed, {"c0": row.get("c0"), "c1": row.get("c1")})
            prod.produce(topic, value=_frame(buf.getvalue(), schema_id), on_delivery=_on_delivery)
        self._flush_checked(prod, topic, errors)

    def _seed_debezium(self, events: list) -> None:
        # Produce each Debezium change event as a nested-envelope Avro frame. The schema
        # (Envelope{op, source, before, after}) is derived per event from its fieldTypes +
        # source keys, mirroring the operator's DebeziumEventBuilder; `after` references
        # `test.Value` by name (Avro forbids redefining the record `before` already
        # inlined). Registered per distinct schema under <topic>-value (frames carry the
        # schema id, so the AvroParser fetches the right one).
        import fastavro
        topic = self.dsn["seed_topic"]
        sr = self._sr_client()
        prod = self._prod()
        errors: list = []
        def _on_delivery(err, _msg):
            if err is not None:
                errors.append(err)
        ids: dict[str, int] = {}
        for ev in events:
            if str(ev.get("enabled", "true")).lower() == "false":
                continue
            schema = _debezium_envelope_schema(ev.get("fieldTypes", {}), ev.get("source") or {})
            schema_str = json.dumps(schema)
            if schema_str not in ids:
                ids[schema_str] = sr.register(f"{topic}-value", schema_str)
            schema_id = ids[schema_str]
            rec = {"op": ev["op"], "source": ev.get("source"),
                   "before": ev.get("before"), "after": ev.get("after")}
            buf = io.BytesIO()
            fastavro.schemaless_writer(buf, fastavro.parse_schema(schema), rec)
            prod.produce(topic, value=_frame(buf.getvalue(), schema_id), on_delivery=_on_delivery)
        self._flush_checked(prod, topic, errors)

    def select_rows(self, topic: str, poll_budget: float = 8.0) -> list[dict]:
        return self._select(topic, poll_budget, lambda message: self._decode(message.value()))

    def select_records(self, topic: str, poll_budget: float = 8.0) -> list[dict]:
        """Read JSON keys and Avro values together, without stringifying nested data."""
        return self._select(topic, poll_budget, self._decode_record, strict=True)

    def _select(self, topic: str, poll_budget: float, decode_message, *, strict: bool = False) -> list[dict]:
        # poll_budget bounds ONE read of currently-available messages — NOT the total wait.
        # The assertion tiers (diff/data) already re-poll select_rows in a loop bounded by the
        # manifest `timeout`, so that outer loop is authoritative; a small per-call budget keeps
        # a single read from overrunning a short manifest timeout (the old hardcoded 30s could
        # overrun a `timeout: 15` test ~2x before the outer deadline was even checked).
        from confluent_kafka import TopicPartition
        c = self._consumer(f"slt-read-{topic}-{int(time.monotonic()*1000)}")
        try:
            md = c.list_topics(_check_topic(topic), timeout=10)
            t = md.topics.get(topic)
            if t is None or t.error is not None:
                return []
            # Assign each partition at its LOW watermark and count (hi - lo). Cleared
            # topics start at 0, but a reused/live broker may have a non-zero low
            # (retention/compaction) — assuming 0 would over-count and wait out the
            # timeout, and could seek before the earliest available offset.
            tps, remaining = [], 0
            for p in t.partitions:
                lo, hi = c.get_watermark_offsets(TopicPartition(topic, p), timeout=10, cached=False)
                tps.append(TopicPartition(topic, p, lo))
                remaining += hi - lo
            c.assign(tps)
            rows = []
            deadline = time.monotonic() + poll_budget
            while remaining > 0 and time.monotonic() < deadline:
                msg = c.poll(1.0)
                if msg is None or msg.error() is not None:
                    continue
                rows.extend(decode_message(msg))   # a message may hold >1 frame
                remaining -= 1
            if strict and remaining:
                raise RuntimeError(f"kafka_record snapshot incomplete: {remaining} captured message(s) unread; refusing partial results")
            return rows
        finally:
            c.close()

    def count_rows(self, topic: str) -> int:
        return len(self.select_rows(topic))
