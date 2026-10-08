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
 * Proves {@link IntegrationProcessor#drive} wraps {@code Request.passwordProperties}
 * entries in the mock {@code com.webaction.security.Password} before construction, so a core's {@code instanceof
 * Password} check on a connection property succeeds the same way it would against the
 * real Striim platform type — and that a property named in {@code passwordProperties}
 * with no value, or omitted entirely, is simply left alone. Builds a minimal
 * single-arg-{@code Map} "stub op" jar on the fly (same technique as
 * {@code IntegrationProcessorReservedKeysTest}) that echoes the raw class name and, when
 * it is a {@code Password}, the plaintext, into the emitted event's userdata.
 */
class IntegrationProcessorPasswordTest {

    @Test
    void driveWrapsNamedPropertyInPassword(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of("Secret", "hunter2"), List.of(input), null,
                List.of("Secret"), null, null);

        assertEquals(1, output.size());
        assertEquals("com.webaction.security.Password", output.get(0).userdata.get("seenClass"));
        assertEquals("hunter2", output.get(0).userdata.get("seenPlain"));
    }

    @Test
    void driveLeavesUnnamedPropertyAsPlainString(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        // No passwordProperties named -- the 4-arg overload must still work unchanged
        // for existing callers, with "Secret" left as the plain string it was in props.
        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of("Secret", "hunter2"), List.of(input), null);

        assertEquals(1, output.size());
        assertEquals("java.lang.String", output.get(0).userdata.get("seenClass"));
        assertEquals("hunter2", output.get(0).userdata.get("seenPlain"));
    }

    @Test
    void driveLeavesPresentButEmptyPasswordPropertyUnwrapped(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        // "Secret" is named as a password property and present, but its value is "" --
        // must be left as a plain empty String, not wrapped in a Password. A JDBC pool's
        // "prefix inherits from unprefixed" fallback (JdbcConnectionFactory.resolveProp)
        // decides "unset" with a String-specific isEmpty() check; a Password("") would
        // defeat that fallback and silently authenticate with no password at all.
        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of("Secret", ""), List.of(input), null,
                List.of("Secret"), null, null);

        assertEquals(1, output.size());
        assertEquals("java.lang.String", output.get(0).userdata.get("seenClass"));
        assertEquals("", output.get(0).userdata.get("seenPlain"));
    }

    @Test
    void driveTreatsMissingPasswordPropertyAsAbsent(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        // "Secret" is named as a password property but never present in `properties` --
        // there is nothing to wrap, so the core sees no value at all (not, say, a
        // Password wrapping null).
        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null,
                List.of("Secret"), null, null);

        assertEquals(1, output.size());
        assertEquals("null", output.get(0).userdata.get("seenClass"));
    }

    /**
     * Compiles a tiny {@code com.example.stubop.Processor} (single {@code Map} ctor
     * — the Gold Standard rule) against this module's own compiled classes (so its
     * {@code WAEvent}/{@code Password} references resolve to the same mock classes this
     * test uses) and packs it into a jar whose manifest declares a
     * {@code Striim-Service-Implementation} — the App class named there need not
     * actually exist; {@link IntegrationProcessor} only derives the core's package from it.
     */
    private static File buildStubOpJar(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("src");
        Path classesDir = tempDir.resolve("classes");
        Path pkgDir = srcDir.resolve("com/example/stubop");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        String source = """
                package com.example.stubop;

                import java.util.List;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;
                import com.webaction.security.Password;

                public class Processor {
                    private final Map<String, Object> props;

                    public Processor(Map<String, Object> props) {
                        this.props = props;
                    }

                    public List<WAEvent> processEvent(WAEvent e) {
                        WAEvent copy = WAEvent.makeCopy(e);
                        Object secret = props.get("Secret");
                        copy.putUserdata("seenClass", secret == null ? "null" : secret.getClass().getName());
                        if (secret instanceof Password p) {
                            copy.putUserdata("seenPlain", p.getPlain());
                        } else if (secret instanceof String s) {
                            copy.putUserdata("seenPlain", s);
                        }
                        return List.of(copy);
                    }

                    public void close() {
                    }
                }
                """;
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
