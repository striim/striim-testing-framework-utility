package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.webaction.recovery.Path;
import com.webaction.recovery.Position;
import com.webaction.uuid.UUID;

/** Fixture {@code "position"} parsing and the at-or-equal resume filter. */
class ScriptedPositionsTest {

    private static final String EVENT = "{\"metadata\":{}}";

    private static String event(String position) {
        return "{\"metadata\":{},\"position\":" + position + "}";
    }

    private static String array(String... entries) {
        return "[" + String.join(",", entries) + "]";
    }

    private static long[] ordinals(ScriptedPositions p) {
        long[] out = new long[p.size()];
        for (int i = 0; i < out.length; i++) {
            out[i] = p.ordinal(i);
        }
        return out;
    }

    @Test
    void missingPositionsAreTodaysOneToN() throws Exception {
        ScriptedPositions read = ScriptedPositions.read(array(EVENT, EVENT, EVENT));
        assertFalse(read.explicit());
        assertEquals("[1, 2, 3]", java.util.Arrays.toString(ordinals(read)));
        ScriptedPositions defaults = ScriptedPositions.defaults(3);
        for (int i = 0; i < 3; i++) {
            assertEquals(defaults.ordinal(i), read.ordinal(i));
            assertEquals(ScriptedPositions.DEFAULT_COMPONENT, read.stemma(i, "d").getComponentID());
        }
    }

    @Test
    void explicitAndSharedPositionsAreRead() throws Exception {
        ScriptedPositions read = ScriptedPositions.read(array(
                event("1"), event("1"), event("2"), event("2"), event("3")));
        assertTrue(read.explicit());
        assertEquals(5, read.size());
        long[] got = ordinals(read);
        assertEquals("[1, 1, 2, 2, 3]", java.util.Arrays.toString(got));
        assertEquals(new OrdinalPosition(2), read.stemma(3, "d").getLowSourcePosition());
    }

    @Test
    void anEventWithoutPositionKeepsIndexPlusOne() throws Exception {
        ScriptedPositions read = ScriptedPositions.read(array(event("1"), event("1"), EVENT));
        assertEquals("[1, 1, 3]", java.util.Arrays.toString(ordinals(read)));
    }

    @Test
    void objectFormNamesAPath() throws Exception {
        ScriptedPositions read = ScriptedPositions.read(array(
                event("{\"ordinal\": 1, \"path\": \"a\"}"), event("{\"ordinal\": 1, \"path\": \"b\"}"),
                event("{\"ordinal\": 2, \"path\": \"a\"}")));
        List<UUID> components = read.components();
        assertEquals(components.get(0), components.get(2));
        assertNotEquals(components.get(0), components.get(1));
        assertNotEquals(ScriptedPositions.DEFAULT_COMPONENT, components.get(0));
    }

    @Test
    void invalidPositionsAreRefusedWithTheIndex() {
        for (String bad : new String[] { "0", "-1", "\"3\"", "1.5", "{\"path\": \"a\"}",
                "{\"ordinal\": 1, \"at\": 2}", "{\"ordinal\": 1, \"path\": \"\"}" }) {
            IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                    () -> ScriptedPositions.read(array(EVENT, event(bad))), bad);
            assertTrue(e.getMessage().contains("array index 1"), e.getMessage());
        }
    }

    @Test
    void aPathMayNotGoBackwards() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> ScriptedPositions.read(array(event("2"), event("1"))));
        assertTrue(e.getMessage().contains("below the 2"), e.getMessage());
        // Implicit index + 1 counts: 5 then an implicit 2 goes backwards.
        assertThrows(IllegalArgumentException.class,
                () -> ScriptedPositions.read(array(event("5"), EVENT)));
    }

    @Test
    void separatePathsOrderIndependently() throws Exception {
        ScriptedPositions.read(array(event("{\"ordinal\": 5, \"path\": \"a\"}"),
                event("{\"ordinal\": 1, \"path\": \"b\"}")));
    }

    @Test
    void filterDropsAtOrBelowDurable() throws Exception {
        ScriptedPositions read = ScriptedPositions.read(array(
                event("1"), event("1"), event("2"), event("2"), event("3")));
        Map<UUID, Long> durable = read.durable(new Position(new Path(
                ScriptedPositions.DEFAULT_COMPONENT, "d", new OrdinalPosition(2))));
        boolean[] dropped = new boolean[5];
        for (int i = 0; i < 5; i++) {
            dropped[i] = read.filtered(i, durable);
        }
        assertEquals("[true, true, true, true, false]", java.util.Arrays.toString(dropped));
        assertFalse(read.filtered(0, read.durable(null)));
    }

    @Test
    void withNoNamedPathEveryDurablePathFoldsToTheHighest() throws Exception {
        // As the old index resume: any OrdinalPosition path counts, whatever its component.
        ScriptedPositions read = ScriptedPositions.defaults(4);
        Map<UUID, Long> durable = read.durable(new Position(List.of(
                new Path(new UUID(0L, 7L), "d", new OrdinalPosition(1)),
                new Path(new UUID(0L, 8L), "d", new OrdinalPosition(3)))));
        assertEquals(Map.of(ScriptedPositions.DEFAULT_COMPONENT, 3L), durable);
    }

    @Test
    void indexResumeEqualsPositionResumeForOneToN() {
        // The old rule fed from index `durable`; the filter's first unfiltered index is the same.
        int n = 7;
        ScriptedPositions p = ScriptedPositions.defaults(n);
        for (int fed = 1; fed <= n; fed++) {
            for (long d = 0; d <= fed; d++) {
                Map<UUID, Long> durable = d == 0 ? Map.of()
                        : Map.of(ScriptedPositions.DEFAULT_COMPONENT, d);
                int first = fed;
                for (int i = 0; i < fed; i++) {
                    if (!p.filtered(i, durable)) {
                        first = i;
                        break;
                    }
                }
                assertEquals((int) d, first, "fed " + fed + ", durable " + d);
                for (int i = fed; i < n; i++) {
                    assertFalse(p.filtered(i, durable), "an unfed event is never dropped");
                }
                p.requireReached(durable, fed);
            }
        }
    }

    @Test
    void durableAheadOfTheStreamIsRefused() throws Exception {
        ScriptedPositions p = ScriptedPositions.defaults(5);
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> p.requireReached(Map.of(ScriptedPositions.DEFAULT_COMPONENT, 4L), 3));
        assertTrue(e.getMessage().contains("ahead of the stream"), e.getMessage());

        ScriptedPositions named = ScriptedPositions.read(array(
                event("{\"ordinal\": 1, \"path\": \"a\"}"), event("{\"ordinal\": 1, \"path\": \"b\"}")));
        assertThrows(IllegalStateException.class,
                () -> named.requireReached(Map.of(new UUID(0L, 42L), 1L), 2));
    }
}
