package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;

/** The placeholder fields cover PostgreSQL's 1600-column limit, and every index has its own field. */
class MockSourceFieldsCapacityTest {

    @Test
    void everyIndexBelowTheCapacityHasItsOwnPlaceholder() {
        assertEquals(1600, MockSourceFields.CAPACITY);
        for (int i = 0; i < MockSourceFields.CAPACITY; i++) {
            assertEquals("f" + i, MockSourceFields.field(i).getName());
        }
    }

    @Test
    void anIndexAtTheCapacityNamesTheConstant() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> MockSourceFields.field(MockSourceFields.CAPACITY));
        assertEquals(true, e.getMessage().contains("MockSourceFields.CAPACITY"));
    }
}
