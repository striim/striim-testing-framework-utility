package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.List;

import org.junit.jupiter.api.Test;

/** A created type's field array: real fields named by column, in declaration order. */
class MockSourceFieldsCreatedTypeTest {

    @Test
    void namedFieldsAreRealInstanceFieldsInTheGivenOrder() {
        Field[] fields = MockSourceFields.namedFields(List.of("LENDERDATABASEID", "LOANRECORDID", "OP"));

        assertNotNull(fields, "the JDK compiler is available in the harness JVM");
        assertEquals(3, fields.length);
        assertEquals("LENDERDATABASEID", fields[0].getName());
        assertEquals("LOANRECORDID", fields[1].getName());
        assertEquals("OP", fields[2].getName());
        for (Field f : fields) {
            assertFalse(Modifier.isStatic(f.getModifiers()), f.getName() + " must be an instance field");
        }
    }

    @Test
    void aNameThatIsNotAJavaIdentifierFallsBack() {
        assertNull(MockSourceFields.namedFields(List.of("ID", "has space")));
        assertNull(MockSourceFields.namedFields(List.of()));
        assertNull(MockSourceFields.namedFields(null));
    }

    @Test
    void aCreatedTypeIsRegisteredByItsUuid() {
        Object uuid = new Object();
        assertNull(MockSourceFields.createdFields(uuid));
        MockSourceFields.registerCreated(uuid, List.of("A", "B"));
        assertEquals(List.of("A", "B"), MockSourceFields.createdFields(uuid));
    }
}
