package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.function.IntPredicate;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.Test;

/** Round-trips WAEvent JSON fixtures (docs/INTEGRATION-TESTS.md) through the reader then the writer. */
class WAEventJsonFactoryTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String FIXTURE = """
            [
              {
                "metadata": { "TableName": "CUSTOMER", "OperationName": "INSERT" },
                "data": { "values": [101, "Record01", "a@example.com"], "present": [true, true, true] },
                "userdata": { "source": "test" }
              },
              {
                "metadata": { "TableName": "CUSTOMER", "OperationName": "UPDATE" },
                "before": { "values": [101, "Record01", "old@example.com"], "present": [true, true, true] },
                "data":   { "values": [101, null, null], "present": [true, true, false] }
              }
            ]
            """;

    @Test
    void insertHasDataOnlyAndNoBefore() throws Exception {
        List<WAEvent> events = WAEventJsonFactory.readEvents(FIXTURE);
        WAEvent insert = events.get(0);

        assertNull(insert.before, "INSERT fixture omits 'before'; the mock WAEvent must leave it null");
        assertNotNull(insert.data);
        assertEquals(101, insert.data[0]);
        assertEquals("Record01", insert.data[1]);
        assertEquals("a@example.com", insert.data[2]);
        assertTrue(insert.isDataPresent(0));
        assertTrue(insert.isDataPresent(1));
        assertTrue(insert.isDataPresent(2));
        assertEquals("test", insert.userdata.get("source"));
    }

    @Test
    void presenceIsDistinctFromNull() throws Exception {
        List<WAEvent> events = WAEventJsonFactory.readEvents(FIXTURE);
        WAEvent update = events.get(1);

        // index 1: present == true, value == null -> a real null column value that IS set.
        assertTrue(update.isDataPresent(1));
        assertNull(update.data[1]);

        // index 2: present == false -> absent, distinct from "present but null".
        assertFalse(update.isDataPresent(2));
        assertNull(update.data[2]);

        // before image is fully present.
        assertTrue(update.isBeforePresent(0));
        assertEquals(101, update.before[0]);
        assertEquals("old@example.com", update.before[2]);
    }

    @Test
    void roundTripsThroughJsonAndBackToEquivalentEvents() throws Exception {
        List<WAEvent> events = WAEventJsonFactory.readEvents(FIXTURE);

        String roundTripped = WAEventJsonFactory.writeEvents(events);
        List<WAEvent> reRead = WAEventJsonFactory.readEvents(roundTripped);

        assertEventsEqual(events, reRead);

        // Also assert directly on the written JSON shape (not just re-reading it through our
        // own reader) so the writer's "present[] from the bitmap, not value != null" contract
        // is genuinely exercised.
        JsonNode tree = MAPPER.readTree(roundTripped);
        JsonNode updateData = tree.get(1).get("data");
        assertTrue(updateData.get("present").get(1).asBoolean(), "index 1 must be present (value is null but set)");
        assertTrue(updateData.get("values").get(1).isNull());
        assertFalse(updateData.get("present").get(2).asBoolean(), "index 2 must be absent");

        JsonNode insertNode = tree.get(0);
        assertFalse(insertNode.has("before"), "writer must omit 'before' when the event has none");
    }

    private static void assertEventsEqual(List<WAEvent> expected, List<WAEvent> actual) {
        assertEquals(expected.size(), actual.size(), "event count mismatch");
        for (int i = 0; i < expected.size(); i++) {
            WAEvent e = expected.get(i);
            WAEvent a = actual.get(i);
            assertEquals(e.metadata, a.metadata, "metadata mismatch at event " + i);
            assertEquals(e.userdata, a.userdata, "userdata mismatch at event " + i);
            assertImageEquals(e.data, e::isDataPresent, a.data, a::isDataPresent, "data", i);
            assertImageEquals(e.before, e::isBeforePresent, a.before, a::isBeforePresent, "before", i);
        }
    }

    private static void assertImageEquals(Object[] expectedValues, IntPredicate expectedPresent, Object[] actualValues, IntPredicate actualPresent, String section,
            int eventIndex) {
        if (expectedValues == null) {
            assertNull(actualValues, section + " should be null at event " + eventIndex);
            return;
        }
        assertNotNull(actualValues, section + " should not be null at event " + eventIndex);
        assertEquals(expectedValues.length, actualValues.length, section + " length mismatch at event " + eventIndex);
        for (int i = 0; i < expectedValues.length; i++) {
            assertEquals(expectedPresent.test(i), actualPresent.test(i), section + " presence mismatch at event " + eventIndex + " column " + i);
            assertEquals(expectedValues[i], actualValues[i], section + " value mismatch at event " + eventIndex + " column " + i);
        }
    }

    // ------------------------------------------------------------- absent slot holding a value

    @Test
    void anAbsentSlotKeepsItsValueWithItsBitClearAndRoundTrips() throws Exception {
        String fixture = """
                [ { "metadata": { "TableName": "T", "OperationName": "UPDATE" },
                    "data":   { "values": [1, "new", "unchanged-but-sent"], "present": [true, true, false] },
                    "before": { "values": [1, "old", null], "present": [true, true, false] } } ]
                """;
        WAEvent e = WAEventJsonFactory.readEvents(fixture).get(0);

        assertFalse(e.isDataPresent(2), "an absent slot's bit stays clear");
        assertEquals("unchanged-but-sent", e.data[2], "an absent slot keeps the value the fixture gave it");
        assertFalse(e.isBeforePresent(2));
        assertNull(e.before[2], "an absent slot written null stays null");

        JsonNode data = MAPPER.readTree(WAEventJsonFactory.writeEvents(List.of(e))).get(0).get("data");
        assertEquals("unchanged-but-sent", data.get("values").get(2).asText());
        assertFalse(data.get("present").get(2).asBoolean());
    }

    // ------------------------------------------------------------- $hex (binary)

    private static final String HEX_FIXTURE = """
            [
              {
                "metadata": { "TableName": "T_BIN", "OperationName": "INSERT" },
                "data": { "values": [1, { "$hex": "00010203fffefdca7e" }], "present": [true, true] }
              },
              {
                "metadata": { "TableName": "T_BIN", "OperationName": "INSERT" },
                "data": { "values": [2, { "$hex": "" }], "present": [true, true] }
              },
              {
                "metadata": { "TableName": "T_BIN", "OperationName": "INSERT" },
                "data": { "values": [3, null], "present": [true, true] }
              }
            ]
            """;

    /**
     * JSON has no byte type, so before this form every value a fixture could feed was a string, a
     * number, a boolean or null -- binary was inexpressible rather than merely untested.
     */
    @Test
    void hexFormDecodesToBytesThatAreNotValidText() throws Exception {
        List<WAEvent> events = WAEventJsonFactory.readEvents(HEX_FIXTURE);

        Object v = events.get(0).data[1];
        assertTrue(v instanceof byte[], "expected a byte[], got " + (v == null ? "null" : v.getClass()));
        assertArrayEquals(new byte[]{0x00, 0x01, 0x02, 0x03, (byte) 0xff, (byte) 0xfe, (byte) 0xfd,
                                     (byte) 0xca, 0x7e}, (byte[]) v);
    }

    @Test
    void hexFormDistinguishesZeroLengthFromNull() throws Exception {
        List<WAEvent> events = WAEventJsonFactory.readEvents(HEX_FIXTURE);

        Object empty = events.get(1).data[1];
        assertTrue(empty instanceof byte[]);
        assertEquals(0, ((byte[]) empty).length, "a zero-length value is not an absent one");

        assertNull(events.get(2).data[1]);
        assertTrue(events.get(2).isDataPresent(1), "null-but-present, as elsewhere in this format");
    }

    @Test
    void aBareStringStaysAString() throws Exception {
        // The reserved-key object exists precisely so a CHAR column holding "cafe01" is not
        // guessed into bytes. Nothing about a bare string may change.
        String fixture = """
                [ { "metadata": { "TableName": "T", "OperationName": "INSERT" },
                    "data": { "values": ["cafe01"], "present": [true] } } ]
                """;
        assertEquals("cafe01", WAEventJsonFactory.readEvents(fixture).get(0).data[0]);
    }

    @Test
    void hexIsDecodedStrictly() {
        // A short byte array surfaces much later as a row that does not match, naming nothing.
        // Refusing at parse time names the fixture instead.
        for (String bad : new String[]{"0", "abc", "zz", "00ff0"}) {
            String fixture = """
                    [ { "metadata": { "TableName": "T", "OperationName": "INSERT" },
                        "data": { "values": [ { "$hex": "%s" } ], "present": [true] } } ]
                    """.formatted(bad);
            assertThrows(IllegalArgumentException.class,
                    () -> WAEventJsonFactory.readEvents(fixture),
                    "\"" + bad + "\" is not a byte string and must be refused, not truncated");
        }
    }

    @Test
    void bytesAreWrittenBackAsHexNotBase64() throws Exception {
        // Jackson renders a byte[] as base64 by default, so without a symmetric writer a fixture
        // round trip would not return what it stated -- and a base64 rendering of bytes is exactly
        // what one earlier measurement mistook for stored data.
        List<WAEvent> events = WAEventJsonFactory.readEvents(HEX_FIXTURE);
        JsonNode out = MAPPER.readTree(WAEventJsonFactory.writeEvents(events));

        JsonNode value = out.get(0).get("data").get("values").get(1);
        assertTrue(value.isObject(), "expected the $hex object form, got " + value);
        assertEquals("00010203fffefdca7e", value.get("$hex").asText());
        assertEquals("", out.get(1).get("data").get("values").get(1).get("$hex").asText());
        assertTrue(out.get(2).get("data").get("values").get(1).isNull());
    }
}
