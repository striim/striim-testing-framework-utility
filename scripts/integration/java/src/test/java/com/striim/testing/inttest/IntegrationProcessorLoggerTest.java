package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.HashMap;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.striim.testing.inttest.loggerfixtures.current.Logger;

/**
 * {@link IntegrationProcessor#newLogger} builds the injected {@code Logger} from the case's
 * {@code EnableLogging} and {@code LogSink}, as an App does, against fixture classes shaped like
 * OpenProcessorCommon's.
 */
class IntegrationProcessorLoggerTest {

    @Test
    void enableLoggingAndLogSinkReachTheLogger() throws Exception {
        Logger logger = (Logger) IntegrationProcessor.newLogger(Logger.class,
                Map.of("EnableLogging", "true", "LogSink", "log4j"));

        assertEquals("inttest", logger.tag);
        assertTrue(logger.enableDebugLogging);
        assertEquals("log4j", logger.selection.property);
        assertNull(logger.selection.moduleDefault, "the harness cannot see a module's own default");
    }

    @Test
    void keysMatchIgnoringCaseAsOnANode() throws Exception {
        Logger logger = (Logger) IntegrationProcessor.newLogger(Logger.class,
                Map.of("enablelogging", true, "logsink", "stream"));

        assertTrue(logger.enableDebugLogging);
        assertEquals("stream", logger.selection.property);
    }

    @Test
    void absentOrNullPropertiesGiveTheDefaults() throws Exception {
        Logger none = (Logger) IntegrationProcessor.newLogger(Logger.class, null);
        assertFalse(none.enableDebugLogging);
        assertNull(none.selection.property);

        Map<String, Object> empty = new HashMap<>();
        Logger unset = (Logger) IntegrationProcessor.newLogger(Logger.class, empty);
        assertFalse(unset.enableDebugLogging);
        assertNull(unset.selection.property);
    }

    @Test
    void aCommonWithoutLogSinkSelectionGetsTheTwoArgConstructor() throws Exception {
        var logger = (com.striim.testing.inttest.loggerfixtures.legacy.Logger) IntegrationProcessor.newLogger(
                com.striim.testing.inttest.loggerfixtures.legacy.Logger.class, Map.of("EnableLogging", "true"));

        assertEquals("inttest", logger.tag);
        assertTrue(logger.enableDebugLogging);
    }
}
