package com.webaction.runtime.monitor;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

/** {@code Type.name()} is the platform's accessor; code compiled against the platform calls it. */
class MonitorEventTypeTest {

    @Test
    void nameMatchesGetName() {
        assertEquals("PROCESSED", MonitorEvent.Type.PROCESSED.name());
        assertEquals(MonitorEvent.Type.COMMIT_LAG.getName(), MonitorEvent.Type.COMMIT_LAG.name());
    }

    @Test
    void nameIsPublicNoArgString() throws NoSuchMethodException {
        assertEquals(String.class, MonitorEvent.Type.class.getMethod("name").getReturnType());
    }
}
