package com.example;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assertions.fail;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

/**
 * Walks every fixture under {@code examples/} and asserts the real ReferenceUdf function turns the
 * fixture input into the fixture output. This is the UDF analog of the OP config-parse guard fused
 * with golden output: the examples are documentation AND a regression net, and cannot rot.
 *
 * <p>Each {@code examples/<Function>/} directory holds {@code input.txt} (seeded as
 * {@code data[0]} of a synthetic {@link WAEvent}), {@code expected.txt} (the expected
 * {@code data[0]} of the returned copy — every reference function here is a
 * copy-adds-userdata transform, so {@code data[]} passthrough is always checked alongside the
 * {@code userdata.processed=true} stamp, unconditionally), and {@code app.tql}/{@code ddl.sql}/
 * {@code seed.sql} (a self-contained runnable example; not executed by this hermetic guard).
 *
 * <p>The {@link #HANDLERS} table maps each fixture directory to the type-correct invocation. The
 * guard asserts a bijection between the handlers and the fixture directories, so adding a function
 * fixture without wiring it (or wiring one without a fixture) fails the build.
 */
class ExamplesGuardTest {

    @FunctionalInterface
    private interface Fn {
        WAEvent apply(WAEvent input);
    }

    private static final Map<String, Fn> HANDLERS = new LinkedHashMap<>();
    static {
        HANDLERS.put("ReferenceUdfMarkProcessed", ReferenceUdf::ReferenceUdfMarkProcessed);
    }

    private static String read(final File f) {
        try {
            return new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
        } catch (final Exception ex) {
            throw new RuntimeException("failed to read " + f, ex);
        }
    }

    private static WAEvent eventWithData(final String value) {
        final WAEvent e = new WAEvent(1, UUID.genCurTimeUUID());
        e.data = new Object[] { value };
        return e;
    }

    @Test
    void everyExampleMatchesItsHandler() {
        final File examplesDir = new File("examples");
        final File[] fixtureDirs = examplesDir.listFiles(File::isDirectory);
        assertTrue(fixtureDirs != null && fixtureDirs.length > 0, "no examples/<Function>/ fixture dirs found");

        for (final File dir : fixtureDirs) {
            final String name = dir.getName();
            final Fn fn = HANDLERS.get(name);
            if (fn == null) {
                fail("examples/" + name + " has no ExamplesGuardTest.HANDLERS entry");
                continue;
            }
            final File inputFile = new File(dir, "input.txt");
            final File expectedFile = new File(dir, "expected.txt");
            assertTrue(inputFile.isFile(), name + "/input.txt missing");
            assertTrue(expectedFile.isFile(), name + "/expected.txt missing");

            final WAEvent copy = fn.apply(eventWithData(read(inputFile)));
            assertEquals(read(expectedFile), String.valueOf(copy.data[0]), name + " fixture mismatch");
            assertEquals("true", copy.userdata.get("processed"), name + " did not stamp userdata.processed=true");
        }

        for (final String handlerName : HANDLERS.keySet()) {
            assertTrue(new File(examplesDir, handlerName).isDirectory(),
                    "HANDLERS has '" + handlerName + "' but examples/" + handlerName + "/ does not exist");
        }
    }
}
