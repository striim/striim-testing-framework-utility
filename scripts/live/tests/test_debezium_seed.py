import io
import json
import fastavro
from livetest.kafkaadmin import _debezium_envelope_schema

# The Debezium-envelope Avro seeding for AvroConverterOp. Verifies the schema shape and
# that a nested event round-trips through fastavro with the exact schema the seeder
# registers + encodes with (the AvroParser fetches the same schema by id at runtime).

def test_envelope_schema_shape():
    s = _debezium_envelope_schema({"id": "int", "name": "string"},
                                  {"db": "mydb", "table": "customers"})
    assert s["name"] == "Envelope" and s["namespace"] == "test"
    by = {f["name"]: f for f in s["fields"]}
    assert by["op"]["type"] == "string"
    # before inlines the Value record; after references it by name (Avro can't redefine it)
    assert by["before"]["type"][1]["name"] == "Value"
    assert by["after"]["type"] == ["null", "test.Value"]
    # Value fields are optional + typed per fieldTypes
    vfields = {f["name"]: f["type"] for f in by["before"]["type"][1]["fields"]}
    assert vfields["id"] == ["null", "int"] and vfields["name"] == ["null", "string"]
    # Source fields are optional strings
    sfields = {f["name"]: f["type"] for f in by["source"]["type"][1]["fields"]}
    assert sfields["db"] == ["null", "string"] and sfields["table"] == ["null", "string"]

def test_envelope_roundtrips_through_fastavro():
    schema = _debezium_envelope_schema({"id": "int", "name": "string"},
                                       {"db": "mydb", "table": "customers"})
    parsed = fastavro.parse_schema(schema)
    rec = {"op": "c", "source": {"db": "mydb", "table": "customers"},
           "before": None, "after": {"id": 1, "name": "Record01"}}
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, parsed, rec)
    buf.seek(0)
    got = fastavro.schemaless_reader(buf, parsed)
    assert got["op"] == "c"
    assert got["after"] == {"id": 1, "name": "Record01"}
    assert got["source"]["table"] == "customers"
    assert got["before"] is None

def test_envelope_schema_is_valid_json():
    s = _debezium_envelope_schema({"amount": "double"}, {"table": "orders"})
    json.dumps(s)   # registered as a schema string — must serialize
