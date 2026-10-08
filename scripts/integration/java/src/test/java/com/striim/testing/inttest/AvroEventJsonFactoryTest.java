package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.ByteBuffer;
import java.util.List;
import java.util.Map;

import com.webaction.event.Event;
import com.webaction.proc.events.AvroEvent;
import com.webaction.proc.events.WAEvent;
import org.apache.avro.generic.GenericRecord;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

/**
 * The {@code "kind": "avro"} fixture reader: plain JSON in, an Avro datum out.
 *
 * <p>The conversion is where a case's meaning can silently drift from what its author wrote — a
 * union taking the wrong branch, or a mistyped field name quietly dropped — so the cases below
 * lean on the failures as much as the successes.</p>
 */
class AvroEventJsonFactoryTest {

    private static String fixture(String schema, String record, String extra) {
        return "[{ \"kind\": \"avro\", \"schema\": " + schema + ", \"record\": " + record
                + (extra == null ? "" : ", " + extra) + " }]";
    }

    private static AvroEvent readOne(String schema, String record) throws Exception {
        return readOne(schema, record, null);
    }

    private static AvroEvent readOne(String schema, String record, String extra) throws Exception {
        List<Event> events = WAEventJsonFactory.readInputEvents(fixture(schema, record, extra));
        assertEquals(1, events.size());
        return assertInstanceOf(AvroEvent.class, events.get(0));
    }

    private static final String SCALARS = """
            { "type": "record", "name": "R", "fields": [
                { "name": "i", "type": "int" },
                { "name": "l", "type": "long" },
                { "name": "f", "type": "float" },
                { "name": "d", "type": "double" },
                { "name": "b", "type": "boolean" },
                { "name": "s", "type": "string" } ] }
            """;

    @Test
    @DisplayName("scalars arrive as the Java types the schema names, not as whatever JSON had")
    void readsScalars() throws Exception {
        AvroEvent event = readOne(SCALARS,
                """
                { "i": 7, "l": 8, "f": 1.5, "d": 2.5, "b": true, "s": "x" }
                """);
        GenericRecord r = event.getData();
        assertEquals(7, r.get("i"));
        assertEquals(8L, r.get("l"), "a JSON integer against a long schema must not stay an Integer");
        assertEquals(1.5f, r.get("f"));
        assertEquals(2.5d, r.get("d"));
        assertEquals(true, r.get("b"));
        assertEquals("x", r.get("s"));
    }

    private static final String NULLABLE = """
            { "type": "record", "name": "R", "fields": [
                { "name": "v", "type": ["null", "int"], "default": null } ] }
            """;

    @Test
    @DisplayName("a nullable union takes its null branch for null and its value branch otherwise")
    void resolvesNullableUnions() throws Exception {
        assertNull(readOne(NULLABLE, "{ \"v\": null }").getData().get("v"));
        assertEquals(3, readOne(NULLABLE, "{ \"v\": 3 }").getData().get("v"));
    }

    @Test
    @DisplayName("a nested record, and a named type REUSED by a later field, both resolve")
    void readsNestedAndReusedRecords() throws Exception {
        String schema = """
                { "type": "record", "name": "Env", "fields": [
                    { "name": "after", "type": ["null",
                        { "type": "record", "name": "Value", "fields": [
                            { "name": "id", "type": ["null", "int"], "default": null } ] }], "default": null },
                    { "name": "before", "type": ["null", "Value"], "default": null } ] }
                """;
        GenericRecord r = readOne(schema, "{ \"after\": { \"id\": 1 }, \"before\": { \"id\": 0 } }").getData();
        assertEquals(1, ((GenericRecord) r.get("after")).get("id"));
        assertEquals(0, ((GenericRecord) r.get("before")).get("id"),
                "`before` reuses `Value` by name, which is how Debezium emits it");
    }

    @Test
    @DisplayName("arrays, maps, enums, bytes and fixed all convert")
    void readsCompoundTypes() throws Exception {
        String schema = """
                { "type": "record", "name": "R", "fields": [
                    { "name": "arr", "type": { "type": "array", "items": "int" } },
                    { "name": "map", "type": { "type": "map", "values": "string" } },
                    { "name": "en", "type": { "type": "enum", "name": "E", "symbols": ["A", "B"] } },
                    { "name": "by", "type": "bytes" },
                    { "name": "fx", "type": { "type": "fixed", "name": "F", "size": 2 } } ] }
                """;
        GenericRecord r = readOne(schema,
                """
                { "arr": [1, 2], "map": { "k": "v" }, "en": "B", "by": "AQI=", "fx": "AQI=" }
                """).getData();
        assertEquals(List.of(1, 2), r.get("arr"));
        assertEquals(Map.of("k", "v"), r.get("map"));
        assertEquals("B", r.get("en").toString());
        assertEquals(ByteBuffer.wrap(new byte[] { 1, 2 }), r.get("by"));
        assertEquals(2, ((org.apache.avro.generic.GenericData.Fixed) r.get("fx")).bytes().length);
    }

    @Test
    @DisplayName("metadata and userdata land on the AvroEvent, not in the record")
    void readsEnvelopeMaps() throws Exception {
        AvroEvent event = readOne(NULLABLE, "{ \"v\": 1 }",
                "\"metadata\": { \"KafkaRecordTimestamp\": 17 }, \"userdata\": { \"u\": \"y\" }");
        assertEquals(17, event.metadata.get("KafkaRecordTimestamp"));
        assertEquals("y", event.userdata.get("u"));
    }

    @Test
    @DisplayName("a field the schema does not declare is REJECTED, never dropped")
    void rejectsAnUndeclaredField() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> readOne(NULLABLE, "{ \"v\": 1, \"typo\": 2 }"));
        // Dropping it would read as "the operator ignored my column", which is a real defect's
        // symptom, and the case would assert against a record it never built.
        assertTrue(e.getMessage().contains("typo"), e.getMessage());
    }

    @Test
    @DisplayName("a value of the wrong type names the position, not just the type")
    void reportsWhereAMismatchIs() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> readOne(SCALARS, """
                        { "i": "not an int", "l": 1, "f": 1, "d": 1, "b": true, "s": "x" }
                        """));
        assertTrue(e.getMessage().contains("record.i"), e.getMessage());
    }

    @Test
    @DisplayName("null against a union with no null branch is an error, not a silent null")
    void rejectsNullWhereTheUnionForbidsIt() {
        String schema = """
                { "type": "record", "name": "R", "fields": [
                    { "name": "v", "type": ["int", "string"] } ] }
                """;
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> readOne(schema, "{ \"v\": null }"));
        assertTrue(e.getMessage().contains("no null branch"), e.getMessage());
    }

    @Test
    @DisplayName("a typo inside a nullable union's only branch reports THAT, not 'fits no branch'")
    void reportsTheBranchsOwnErrorWhenAUnionHasOneCandidate() {
        // ["null", "Value"] is what Debezium emits for every envelope field, so a typo inside the
        // branch is the commonest fixture mistake there is. Swallowing the branch's exception to
        // report the whole union would bury the useful half.
        String schema = """
                { "type": "record", "name": "Env", "fields": [
                    { "name": "after", "type": ["null",
                        { "type": "record", "name": "Value", "fields": [
                            { "name": "id", "type": ["null", "int"], "default": null } ] }],
                      "default": null } ] }
                """;
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> readOne(schema, "{ \"after\": { \"idd\": 1 } }"));
        assertTrue(e.getMessage().contains("'idd' is not a field of record"), e.getMessage());
        assertTrue(e.getMessage().contains("record.after"), e.getMessage());
    }

    @Test
    @DisplayName("a genuinely ambiguous union still reports the union when nothing fits")
    void reportsTheUnionWhenSeveralBranchesAllFail() {
        String schema = """
                { "type": "record", "name": "R", "fields": [
                    { "name": "v", "type": ["int", "string"] } ] }
                """;
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> readOne(schema, "{ \"v\": true }"));
        assertTrue(e.getMessage().contains("fits no branch of union"), e.getMessage());
    }

    @Test
    @DisplayName("an ABSENT field is left null -- Avro schema defaults are not applied")
    void leavesAnAbsentFieldNull() throws Exception {
        // Pins the real behaviour of GenericData.Record rather than what "default": null in the
        // schema might suggest: nothing here reads Schema.Field.defaultVal().
        String schema = """
                { "type": "record", "name": "R", "fields": [
                    { "name": "a", "type": ["null", "int"], "default": null },
                    { "name": "b", "type": ["null", "int"], "default": null } ] }
                """;
        GenericRecord r = readOne(schema, "{ \"a\": 1 }").getData();
        assertEquals(1, r.get("a"));
        assertNull(r.get("b"), "absent in the fixture, so null in the record");
    }

    @Test
    @DisplayName("a fixture array may mix kinds, and each entry becomes its own event type")
    void mixesKinds() throws Exception {
        String json = "[" + """
                { "metadata": { "TableName": "T" }, "data": { "values": [1], "present": [true] } },
                """ + "{ \"kind\": \"avro\", \"schema\": " + NULLABLE + ", \"record\": { \"v\": 1 } }]";
        List<Event> events = WAEventJsonFactory.readInputEvents(json);
        assertEquals(2, events.size());
        assertInstanceOf(WAEvent.class, events.get(0));
        assertInstanceOf(AvroEvent.class, events.get(1));
    }

    @Test
    @DisplayName("readEvents (WAEvent-only) refuses an avro entry rather than mis-reading it")
    void waEventReaderRefusesAvro() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> WAEventJsonFactory.readEvents(fixture(NULLABLE, "{ \"v\": 1 }", null)));
        assertTrue(e.getMessage().contains("input-only"), e.getMessage());
    }
}
