package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import com.webaction.proc.events.WAEvent;
import com.webaction.runtime.BuiltInFunc;

import org.junit.jupiter.api.Test;

/**
 * {@code BuiltInFunc.to_WAEvent} — the mock of the platform function a reader uses to rebuild the
 * event a stored payload came from.
 *
 * <p><b>This parses the PRODUCTION dialect, which is not the harness's fixture dialect.</b> A stored
 * payload is written by the product as {@code "data":["1","ACME"]}; a fixture writes
 * {@code "data":{"values":[...],"present":[...]}} so it can express column presence. Delegating to
 * the fixture reader was tried first and made a real case fail — hence these tests pin the dialect
 * itself, not just the happy path.</p>
 */
class BuiltInFuncToWaEventTest {

    @Test
    void parsesMetadataAndColumnsFromTheProductionDialect() throws Exception {
        List<WAEvent> events = BuiltInFunc.to_WAEvent(
                "[{\"metadata\":{\"TableName\":\"HR.EMP\",\"OperationName\":\"INSERT\"},"
                        + "\"data\":[\"1\",\"ACME\"]}]");

        assertEquals(1, events.size());
        WAEvent event = events.get(0);
        assertEquals("HR.EMP", event.metadata.get("TableName"));
        assertEquals("INSERT", event.metadata.get("OperationName"));
        assertEquals("1", event.data[0]);
        assertEquals("ACME", event.data[1]);
    }

    @Test
    void everyParsedColumnIsMarkedPRESENT() throws Exception {
        // Written through setData, never `data[i] =`. A column that reads as absent is the classic
        // silent-drop bug, and here the harness would be the one causing it.
        List<WAEvent> events = BuiltInFunc.to_WAEvent(
                "[{\"metadata\":{},\"data\":[\"a\",\"b\",\"c\"]}]");

        WAEvent event = events.get(0);
        for (int i = 0; i < 3; i++) {
            assertTrue(BuiltInFunc.IS_PRESENT(event, event.data, i), "column " + i + " must be present");
        }
    }

    @Test
    void aNullColumnIsPresentWithANullValueNotAbsent() throws Exception {
        // The production dialect has NO presence array, so a serialized image cannot express
        // absence: every column it lists was written. An earlier revision skipped nulls, which
        // reconstructed them as ABSENT -- silent data loss inside the harness.
        List<WAEvent> events = BuiltInFunc.to_WAEvent("[{\"metadata\":{},\"data\":[\"a\",null]}]");

        WAEvent event = events.get(0);
        assertNull(event.data[1]);
        assertTrue(BuiltInFunc.IS_PRESENT(event, event.data, 1),
                "a null column is present with a null value, not absent");
    }

    @Test
    void theBeforeImageIsParsedToo() throws Exception {
        List<WAEvent> events = BuiltInFunc.to_WAEvent(
                "[{\"metadata\":{},\"data\":[\"new\"],\"before\":[\"old\"]}]");

        WAEvent event = events.get(0);
        assertEquals("new", event.data[0]);
        assertEquals("old", event.before[0]);
        assertTrue(BuiltInFunc.IS_PRESENT(event, event.before, 0));
    }

    @Test
    void everyEventInTheArrayIsReturnedInOrder() throws Exception {
        List<WAEvent> events = BuiltInFunc.to_WAEvent(
                "[{\"metadata\":{},\"data\":[\"first\"]},{\"metadata\":{},\"data\":[\"second\"]}]");

        assertEquals(2, events.size());
        assertEquals("first", events.get(0).data[0]);
        assertEquals("second", events.get(1).data[0]);
    }

    @Test
    void anEventWithNoDataImageIsNotAFailure() throws Exception {
        // A stored payload may carry metadata only; that is a reconstructable event with no columns,
        // not a parse error.
        List<WAEvent> events = BuiltInFunc.to_WAEvent("[{\"metadata\":{\"TableName\":\"T\"}}]");

        assertEquals(1, events.size());
        assertNull(events.get(0).data);
    }

    @Test
    void aNonArrayPayloadFailsSayingWhat() throws Exception {
        // The real function takes a serialized ARRAY; a bare object is an authoring/store error and
        // must not be silently treated as empty, which would emit nothing and look like "no events".
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> BuiltInFunc.to_WAEvent("{\"metadata\":{}}"));

        assertTrue(e.getMessage().contains("expects a JSON array"), e.getMessage());
    }
}
