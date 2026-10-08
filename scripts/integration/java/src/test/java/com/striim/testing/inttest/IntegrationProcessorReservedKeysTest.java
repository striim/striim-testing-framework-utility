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
 * Proves {@link IntegrationProcessor#drive} injects the reserved
 * {@code striim.op.namespace}/{@code striim.op.sourceName} props keys (mirroring
 * {@code AbstractOpenProcessorApp.NAMESPACE_KEY}/{@code SOURCE_NAME_KEY} in
 * OpenProcessorCommon, which this harness module never compiles against — hence the
 * literal keys duplicated in {@code IntegrationProcessor.resolveConstructorArgument}).
 * Builds a minimal single-arg-{@code Map} "stub op" jar on the fly (no OpenProcessorCommon
 * dependency needed — a type-creating core only needs the {@code Map} ctor parameter to see
 * the keys) that echoes the two props values back into the emitted event's userdata, so the
 * test can assert on them without any reflection into the child classloader.
 */
class IntegrationProcessorReservedKeysTest {

    @Test
    void driveInjectsRequestNamespaceAndSourceNameIntoProps(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null, "myNamespace", "mySource");

        assertEquals(1, output.size());
        assertEquals("myNamespace", output.get(0).userdata.get("seenNamespace"));
        assertEquals("mySource", output.get(0).userdata.get("seenSourceName"));
    }

    @Test
    void driveFallsBackToHarnessDefaultsWhenRequestOmitsThem(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);

        WAEvent input = new WAEvent();

        // The 4-arg overload (no namespace/sourceName) — must still work unchanged for
        // existing callers (e.g. IntegrationProcessorReferenceOpTest), falling back to
        // the harness defaults.
        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null);

        assertEquals(1, output.size());
        assertEquals("inttest", output.get(0).userdata.get("seenNamespace"));
        assertEquals("source", output.get(0).userdata.get("seenSourceName"));
    }

    /**
     * Compiles a tiny {@code com.example.stubop.Processor} (single {@code Map} ctor
     * — the Gold Standard rule) against this module's own compiled classes (so its
     * {@code WAEvent} reference resolves to the same mock class this test uses) and packs it
     * into a jar whose manifest declares a {@code Striim-Service-Implementation} — the App
     * class named there need not actually exist; {@link IntegrationProcessor} only derives the
     * core's package from it.
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

                public class Processor {
                    private final Map<String, Object> props;

                    public Processor(Map<String, Object> props) {
                        this.props = props;
                    }

                    public List<WAEvent> processEvent(WAEvent e) {
                        WAEvent copy = WAEvent.makeCopy(e);
                        copy.putUserdata("seenNamespace", String.valueOf(props.get("striim.op.namespace")));
                        copy.putUserdata("seenSourceName", String.valueOf(props.get("striim.op.sourceName")));
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
