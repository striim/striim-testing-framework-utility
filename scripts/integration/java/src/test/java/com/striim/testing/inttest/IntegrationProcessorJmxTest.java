package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.lang.management.ManagementFactory;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.jar.Attributes;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;
import java.util.stream.Stream;

import javax.management.ObjectName;
import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * {@code assert.jmx}'s Java half: {@link IntegrationProcessor#drive} with a {@link JmxSpec} builds
 * the op's MBean, registers it under production's name, and writes every attribute to the
 * sidecar — and writes an explicit error, never an empty success, when no bean can be built.
 * Stub op jars are compiled on the fly, as in {@link IntegrationProcessorLifecycleTest}.
 */
class IntegrationProcessorJmxTest {

    private static final String PKG = "com.example.StubJmxOpV2";

    private static final String PROCESSOR = """
            package com.example.StubJmxOpV2;

            import java.util.List;
            import java.util.Map;

            import com.webaction.proc.events.WAEvent;

            public class Processor {
                long seen;
                boolean closed;

                public Processor(Map<String, Object> props) {
                }

                public List<WAEvent> processEvent(WAEvent e) {
                    seen++;
                    return List.of(e);
                }

                public void close() {
                    closed = true;
                }
            }
            """;

    private static final String MXBEAN = """
            package com.example.StubJmxOpV2;

            public interface CounterMXBean {
                long getSeen();

                boolean isGateRunning();

                boolean isClosed();
            }
            """;

    private static final String VIEW = """
            package com.example.StubJmxOpV2;

            import java.util.function.Supplier;

            public final class CounterMBeanView implements CounterMXBean {
                private final Processor processor;
                private final Supplier<Object> gate;

                public CounterMBeanView(Processor processor, Supplier<Object> gate) {
                    this.processor = processor;
                    this.gate = gate;
                }

                public long getSeen() {
                    return processor.seen;
                }

                public boolean isGateRunning() {
                    return gate.get() != null;
                }

                public boolean isClosed() {
                    return processor.closed;
                }
            }
            """;

    private static final String SECOND_VIEW = """
            package com.example.StubJmxOpV2;

            public final class OtherMBeanView implements CounterMXBean {
                private final Processor processor;

                public OtherMBeanView(Processor processor) {
                    this.processor = processor;
                }

                public long getSeen() {
                    return -1;
                }

                public boolean isGateRunning() {
                    return true;
                }

                public boolean isClosed() {
                    return processor.closed;
                }
            }
            """;

    /** A relocated OpJmxRegistry stand-in; {@code refuse} makes register answer null as production does on failure. */
    private static String registry(boolean refuse) {
        return """
                package com.example.shaded.StubJmxOpV2.common;

                import java.lang.management.ManagementFactory;
                import javax.management.ObjectName;

                public final class OpJmxRegistry {
                    public static String typeFor(Class<?> c) {
                        return "FromRegistry";
                    }

                    public static ObjectName register(Object mbean, String type, String component, Object logger) {
                        if (%s) {
                            return null;
                        }
                        try {
                            ObjectName name = new ObjectName("com.example:type=" + type + ",name=" + ObjectName.quote(component));
                            ManagementFactory.getPlatformMBeanServer().registerMBean(mbean, name);
                            return name;
                        } catch (Exception e) {
                            return null;
                        }
                    }

                    public static void unregister(ObjectName name, Object mbean, Object logger) {
                        try {
                            ManagementFactory.getPlatformMBeanServer().unregisterMBean(name);
                        } catch (Exception ignored) {
                        }
                    }
                }
                """.formatted(refuse);
    }

    @Test
    void discoversRegistersAndSnapshotsTheBeanBeforeClose(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW);

        Map<String, Object> snapshot = driveWithJmx(tempDir, jar, null, 2);

        assertNull(snapshot.get("error"), () -> "unexpected error: " + snapshot);
        // Type without the module version, name "<namespace>.<sourceName>" with the runner's defaults.
        assertEquals("com.example:type=StubJmxOp,name=\"inttest.source\"", snapshot.get("objectName"));
        assertEquals(PKG + ".CounterMBeanView", snapshot.get("beanClass"));
        assertEquals("MBeanServer", snapshot.get("registeredVia"));
        Map<?, ?> attributes = (Map<?, ?>) snapshot.get("attributes");
        assertEquals(2, ((Number) attributes.get("Seen")).intValue());
        assertEquals(false, attributes.get("GateRunning"));
        assertEquals(false, attributes.get("Closed"), "snapshot must be taken before close()");
        assertFalse(ManagementFactory.getPlatformMBeanServer().isRegistered(
                new ObjectName("com.example:type=StubJmxOp,name=\"inttest.source\"")),
                "the bean is unregistered after the snapshot");
    }

    @Test
    void registersThroughTheJarsOwnRegistryWhenItShipsOne(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW, registry(false));

        Map<String, Object> snapshot = driveWithJmx(tempDir, jar, null, 1);

        assertEquals("com.example.shaded.StubJmxOpV2.common.OpJmxRegistry", snapshot.get("registeredVia"),
                () -> "got " + snapshot);
        assertEquals("com.example:type=FromRegistry,name=\"inttest.source\"", snapshot.get("objectName"));
        assertEquals(1, ((Number) ((Map<?, ?>) snapshot.get("attributes")).get("Seen")).intValue());
    }

    @Test
    void aRegistryRefusalIsAnErrorNotAnEmptySnapshot(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW, registry(true));

        Map<String, Object> snapshot = driveWithJmx(tempDir, jar, null, 1);

        assertTrue(String.valueOf(snapshot.get("error")).contains("refused"), () -> "got " + snapshot);
        assertNull(snapshot.get("attributes"));
    }

    @Test
    void noBeanInTheJarIsAnError(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR);

        Map<String, Object> snapshot = driveWithJmx(tempDir, jar, null, 1);

        assertTrue(String.valueOf(snapshot.get("error")).contains("no MBean class found"), () -> "got " + snapshot);
        assertNull(snapshot.get("attributes"));
    }

    @Test
    void twoCandidatesAreRefusedUntilOneIsNamed(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW, SECOND_VIEW);

        Map<String, Object> ambiguous = driveWithJmx(tempDir, jar, null, 1);
        assertTrue(String.valueOf(ambiguous.get("error")).contains("more than one MBean class"),
                () -> "got " + ambiguous);

        Map<String, Object> named = driveWithJmx(tempDir, jar, "OtherMBeanView", 1);
        assertEquals(PKG + ".OtherMBeanView", named.get("beanClass"), () -> "got " + named);
        assertEquals(-1, ((Number) ((Map<?, ?>) named.get("attributes")).get("Seen")).intValue());
    }

    @Test
    void aNamedBeanThatIsNotInTheJarIsAnError(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW);

        Map<String, Object> snapshot = driveWithJmx(tempDir, jar, "NoSuchView", 1);

        assertTrue(String.valueOf(snapshot.get("error")).contains("NoSuchView"), () -> "got " + snapshot);
    }

    @Test
    void jmxIsRefusedForAUdfCase(@TempDir Path tempDir) throws Exception {
        File jar = buildJar(tempDir, PROCESSOR, MXBEAN, VIEW);
        JmxSpec jmx = new JmxSpec();
        jmx.outputFile = tempDir.resolve("jmx.json").toString();

        assertThrows(IllegalArgumentException.class, () -> IntegrationProcessor.drive(jar, Map.of(),
                List.of(), null, null, null, null, new UdfSpec(), null, jmx));
    }

    private static Map<String, Object> driveWithJmx(Path tempDir, File jar, String bean, int events)
            throws Exception {
        Path out = Files.createTempFile(tempDir, "jmx", ".json");
        JmxSpec jmx = new JmxSpec();
        jmx.bean = bean;
        jmx.outputFile = out.toString();
        List<Event> input = new ArrayList<>();
        for (int i = 0; i < events; i++) {
            input.add(new WAEvent());
        }
        List<WAEvent> output = IntegrationProcessor.drive(jar, Map.of(), input, null, null, null, null, null,
                null, jmx);
        assertEquals(events, output.size());
        @SuppressWarnings("unchecked")
        Map<String, Object> snapshot = new ObjectMapper().readValue(out.toFile(), LinkedHashMap.class);
        return snapshot;
    }

    /** Compiles {@code sources} (each a full compilation unit) and packs them into an op jar. */
    private static File buildJar(Path tempDir, String... sources) throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path work = Files.createTempDirectory(tempDir, "stub");
        Path srcDir = work.resolve("src");
        Path classesDir = work.resolve("classes");
        Files.createDirectories(classesDir);
        List<String> args = new ArrayList<>(List.of("-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString()));
        for (String source : sources) {
            String pkg = source.substring(source.indexOf("package ") + 8, source.indexOf(';')).trim();
            int typeAt = source.indexOf("public ", source.indexOf(';'));
            String afterPublic = source.substring(typeAt).replaceFirst(
                    "^public (final )?(class|interface) ", "");
            String type = afterPublic.substring(0, afterPublic.indexOf(' '));
            Path file = srcDir.resolve(pkg.replace('.', '/')).resolve(type + ".java");
            Files.createDirectories(file.getParent());
            Files.writeString(file, source);
            args.add(file.toString());
        }
        int rc = compiler.run(null, null, null, args.toArray(new String[0]));
        if (rc != 0) {
            throw new IllegalStateException("compiling stub op sources failed (exit " + rc + ")");
        }

        Path jarPath = work.resolve("stubjmxop.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation", PKG + ".App");
        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest);
                Stream<Path> classes = Files.walk(classesDir)) {
            for (Path c : (Iterable<Path>) classes.filter(Files::isRegularFile)::iterator) {
                jarOut.putNextEntry(new JarEntry(classesDir.relativize(c).toString().replace(File.separatorChar, '/')));
                Files.copy(c, jarOut);
                jarOut.closeEntry();
            }
        }
        return jarPath.toFile();
    }
}
