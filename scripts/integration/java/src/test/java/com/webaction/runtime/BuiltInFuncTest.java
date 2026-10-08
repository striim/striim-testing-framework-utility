package com.webaction.runtime;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

/**
 * Direct unit tests of the {@code IS_PRESENT}/{@code getIndexOfColumn}/{@code
 * setColumnSchemas} additions ({@code UdfCore} needs these because a UDF can call them
 * as STATIC methods baked into its own bytecode -- no per-instance {@code BuiltInFuncs}
 * proxy seam to intercept, unlike {@code OperatorCore}'s OP path).
 */
class BuiltInFuncTest {

    @AfterEach
    void clearRegistry() {
        // Every test starts from a clean registry -- setColumnSchemas is a REPLACE, but a
        // test that forgets to seed one at all must not see a PRIOR test's schemas either.
        BuiltInFunc.setColumnSchemas(null);
    }

    private static WAEvent eventWithColumns(String table, int columnCount) {
        WAEvent event = new WAEvent();
        event.metadata = new java.util.HashMap<>();
        event.metadata.put("TableName", table);
        event.data = new Object[columnCount];
        event.dataPresenceBitMap = new byte[(columnCount + 7) / 8];
        event.before = new Object[columnCount];
        event.beforePresenceBitMap = new byte[(columnCount + 7) / 8];
        return event;
    }

    // --- IS_PRESENT ---

    @Test
    void isPresentDispatchesToDataByReference() {
        WAEvent event = eventWithColumns("T", 2);
        event.setData(0, "x");
        assertTrue(BuiltInFunc.IS_PRESENT(event, event.data, 0));
        assertFalse(BuiltInFunc.IS_PRESENT(event, event.data, 1));
    }

    @Test
    void isPresentDispatchesToBeforeByReference() {
        WAEvent event = eventWithColumns("T", 2);
        event.setBefore(1, "y");
        assertFalse(BuiltInFunc.IS_PRESENT(event, event.before, 0));
        assertTrue(BuiltInFunc.IS_PRESENT(event, event.before, 1));
    }

    @Test
    void isPresentOutOfRangeIsFalseNotAnException() {
        WAEvent event = eventWithColumns("T", 1);
        assertFalse(BuiltInFunc.IS_PRESENT(event, event.data, 5));
    }

    @Test
    void isPresentNullEventIsFalse() {
        assertFalse(BuiltInFunc.IS_PRESENT(null, new Object[0], 0));
    }

    // --- getIndexOfColumn ---

    @Test
    void getIndexOfColumnResolvesAnExactMatch() {
        BuiltInFunc.setColumnSchemas(Map.of("T", List.of("ID", "NAME")));
        WAEvent event = eventWithColumns("T", 2);
        assertEquals(0, BuiltInFunc.getIndexOfColumn(event, "ID"));
        assertEquals(1, BuiltInFunc.getIndexOfColumn(event, "NAME"));
    }

    @Test
    void getIndexOfColumnMatchesCaseInsensitively() {
        BuiltInFunc.setColumnSchemas(Map.of("T", List.of("ID", "NAME")));
        WAEvent event = eventWithColumns("T", 2);
        assertEquals(1, BuiltInFunc.getIndexOfColumn(event, "name"));
    }

    @Test
    void getIndexOfColumnUnknownColumnIsNegativeOne() {
        BuiltInFunc.setColumnSchemas(Map.of("T", List.of("ID")));
        WAEvent event = eventWithColumns("T", 1);
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(event, "MISSING"));
    }

    @Test
    void getIndexOfColumnUnknownTableIsNegativeOne() {
        BuiltInFunc.setColumnSchemas(Map.of("OTHER", List.of("ID")));
        WAEvent event = eventWithColumns("T", 1);
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(event, "ID"));
    }

    @Test
    void getIndexOfColumnEmptyRegistryIsNegativeOne() {
        WAEvent event = eventWithColumns("T", 1);
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(event, "ID"));
    }

    @Test
    void setColumnSchemasReplacesRatherThanMerges() {
        BuiltInFunc.setColumnSchemas(Map.of("T", List.of("A", "B")));
        BuiltInFunc.setColumnSchemas(Map.of("T", List.of("X")));
        WAEvent event = eventWithColumns("T", 2);
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(event, "A"));
        assertEquals(0, BuiltInFunc.getIndexOfColumn(event, "X"));
    }

    @Test
    void getIndexOfColumnNullEventOrColumnIsNegativeOne() {
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(null, "ID"));
        WAEvent event = eventWithColumns("T", 1);
        assertEquals(-1, BuiltInFunc.getIndexOfColumn(event, null));
    }
}
