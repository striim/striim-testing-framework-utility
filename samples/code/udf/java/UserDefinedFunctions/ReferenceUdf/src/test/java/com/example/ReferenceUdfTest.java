package com.example;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

/**
 * ⚠ COVERAGE GAP: nothing here asserts the {@code SimpleEvent} fields that
 * {@code WAEvents.copyEvent} re-stamps and {@code WAEvent.makeCopy} drops — the set named by
 * {@code WAEvents.DROPPED_BY_MAKECOPY}.
 *
 * <p>So replacing {@code WAEvents.copyEvent} with {@code WAEvent.makeCopy} leaves this suite green
 * while the module silently loses them. {@code key} is the one that matters: {@code SimpleEvent}
 * implements {@code Partitionable}, so dropping it loses the partition assignment.
 *
 * <p>Asserting that {@code key} survives the copy closes this gap.
 */
class ReferenceUdfTest {

    private static WAEvent event(int cols) {
        WAEvent e = new WAEvent(cols, UUID.genCurTimeUUID());
        e.data = new Object[cols];
        return e;
    }

    @AfterEach
    void resetLogging() {
        // Logging state is static; reset after every test to keep tests isolated.
        ReferenceUdf.ReferenceUdfSetLogging(false);
    }

    /** Captures everything written to stdout while running the given action. NOT safe for parallel test execution (replaces the global System.out). */
    private static String captureStdout(final Runnable action) {
        final java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        final java.io.PrintStream original = System.out;
        System.setOut(new java.io.PrintStream(buf));
        try {
            action.run();
        } finally {
            System.setOut(original);
        }
        return buf.toString();
    }

    // ---- null handling ----

    @Test
    void nullInputIsNullSafe() {
        assertNull(ReferenceUdf.ReferenceUdfMarkProcessed(null));
    }

    // ---- copy passthrough ----

    @Test
    void returnsDistinctCopyInstance() {
        WAEvent e = event(1);
        e.data[0] = "v";

        WAEvent copy = ReferenceUdf.ReferenceUdfMarkProcessed(e);

        assertNotSame(e, copy);
    }

    @Test
    void copyPreservesDataArrayValues() {
        WAEvent e = event(3);
        e.data[0] = "x";
        e.data[1] = 42;
        e.data[2] = null;

        WAEvent copy = ReferenceUdf.ReferenceUdfMarkProcessed(e);

        assertArrayEquals(new Object[] { "x", 42, null }, copy.data);
        assertNotSame(e.data, copy.data); // deep copy, not the same array instance
    }

    // ---- processed userdata flag ----

    @Test
    void addsProcessedTrueToUserdata() {
        WAEvent e = event(1);
        e.data[0] = "v";

        WAEvent copy = ReferenceUdf.ReferenceUdfMarkProcessed(e);

        assertEquals("true", copy.userdata.get("processed"));
    }

    @Test
    void doesNotMutateSourceEventUserdata() {
        WAEvent e = event(1);
        e.data[0] = "v";

        ReferenceUdf.ReferenceUdfMarkProcessed(e);

        assertNull(e.userdata); // source untouched; only the copy is stamped
    }

    @Test
    void preservesExistingUserdataAlongsideProcessedFlag() {
        WAEvent e = event(1);
        e.data[0] = "v";
        e.putUserdata("origin", "sourceA");

        WAEvent copy = ReferenceUdf.ReferenceUdfMarkProcessed(e);

        assertEquals("sourceA", copy.userdata.get("origin"));
        assertEquals("true", copy.userdata.get("processed"));
    }

    // ---- Logging toggle (ReferenceUdfSetLogging) ----

    @Test
    void setLoggingReturnsValueSet() {
        assertEquals(true, ReferenceUdf.ReferenceUdfSetLogging(true));
        assertEquals(false, ReferenceUdf.ReferenceUdfSetLogging(false));
    }

    @Test
    void loggingOffByDefaultProducesNoOutput() {
        WAEvent e = event(1);
        e.data[0] = "v";

        final String out = captureStdout(() -> ReferenceUdf.ReferenceUdfMarkProcessed(e));

        assertTrue(out.isEmpty());
    }

    @Test
    void loggingOnTracesInputAndOutput() {
        WAEvent e = event(1);
        e.data[0] = "v";
        ReferenceUdf.ReferenceUdfSetLogging(true);

        final String out = captureStdout(() -> ReferenceUdf.ReferenceUdfMarkProcessed(e));

        assertTrue(out.contains("ReferenceUdfMarkProcessed"));
        assertFalse(out.isEmpty());
    }
}
