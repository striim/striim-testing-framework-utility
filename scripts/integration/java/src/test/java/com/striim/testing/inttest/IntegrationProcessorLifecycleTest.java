package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.jar.Attributes;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import com.webaction.proc.events.WAEvent;

/**
 * Proves {@link IntegrationProcessor#drive} reflectively invokes a no-arg
 * {@code start()} method on the constructed core — mirroring
 * {@code AbstractOpenProcessorApp.start()}'s call to {@code processor.start()}
 * immediately after {@code buildProcessor} (the {@code EventProcessor.start()}
 * default no-op lifecycle hook added in OpenProcessorCommon) — and that it runs
 * before the event-processing loop, and tolerantly (mirroring the existing
 * tolerant {@code close()} invocation) when the core declares no {@code start()}
 * method at all.
 *
 * <p>
 * Builds minimal single-arg-{@code Map} "stub op" jars on the fly (no
 * OpenProcessorCommon dependency needed) whose {@code processEvent} stashes a
 * marker derived from whether {@code start()} ran into the emitted event's
 * userdata, so the test can assert on it without any reflection into the child
 * classloader (same technique as {@code IntegrationProcessorReservedKeysTest}).
 */
class IntegrationProcessorLifecycleTest {

    @Test
    void driveInvokesStartBeforeProcessEvent(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir, true);

        WAEvent input = new WAEvent();

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null);

        assertEquals(1, output.size());
        assertEquals(true, output.get(0).userdata.get("startCalledBeforeProcessEvent"));
    }

    @Test
    void driveToleratesCoreWithNoStartMethod(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir, false);

        WAEvent input = new WAEvent();

        // Not every OP overrides the EventProcessor.start() default no-op; a core with
        // no start() method at all must drive exactly as before (no error, no marker).
        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null);

        assertEquals(1, output.size());
        assertEquals(false, output.get(0).userdata.get("startCalledBeforeProcessEvent"));
    }

    /**
     * Compiles a tiny {@code com.example.stubop.Processor} (single {@code Map} ctor
     * — the Gold Standard rule) against this module's own compiled classes (so its
     * {@code WAEvent} reference resolves to the same mock class this test uses) and packs it
     * into a jar whose manifest declares a {@code Striim-Service-Implementation} — the App
     * class named there need not actually exist; {@link IntegrationProcessor} only derives the
     * core's package from it.
     *
     * @param declareStart when {@code true}, the stub declares a {@code start()} method that
     *     flips a boolean field, observable afterward through {@code processEvent}'s emitted
     *     userdata; when {@code false}, the stub declares no {@code start()} method at all.
     */
    private static File buildStubOpJar(Path tempDir, boolean declareStart) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("src");
        Path classesDir = tempDir.resolve("classes");
        Path pkgDir = srcDir.resolve("com/example/stubop");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        String startMethod = declareStart
                ? "public void start() {\n        started = true;\n    }\n\n    "
                : "";

        String source = """
                package com.example.stubop;

                import java.util.List;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;

                public class Processor {
                    private boolean started;

                    public Processor(Map<String, Object> props) {
                    }

                    %spublic List<WAEvent> processEvent(WAEvent e) {
                        WAEvent copy = WAEvent.makeCopy(e);
                        copy.putUserdata("startCalledBeforeProcessEvent", started);
                        return List.of(copy);
                    }

                    public void close() {
                    }
                }
                """.formatted(startMethod);
        Path sourceFile = pkgDir.resolve("Processor.java");
        Files.writeString(sourceFile, source);

        String classpath = System.getProperty("java.class.path");
        int compileResult = compiler.run(null, null, null,
                "-cp", classpath,
                "-d", classesDir.toString(),
                sourceFile.toString());
        if (compileResult != 0) {
            throw new IllegalStateException("Compiling stub op Processor.java failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("stubop.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation", "com.example.stubop.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            Path classFile = classesDir.resolve("com/example/stubop/Processor.class");
            String entryName = "com/example/stubop/Processor.class";
            jarOut.putNextEntry(new JarEntry(entryName));
            Files.copy(classFile, jarOut);
            jarOut.closeEntry();
        }

        return jarPath.toFile();
    }
}
