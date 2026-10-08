package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.jar.Attributes;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

/**
 * Builds a stub READER jar on the fly, shared by the source tests of both drivers
 * ({@link IntegrationProcessorSourceTest}, {@link PerformanceProcessorSourceTest}).
 *
 * <p>One jar covers every shape the tests need, because the stub's behaviour comes entirely from
 * case properties:</p>
 *
 * <ul>
 *   <li>{@code EventsPerTick} — how many events each tick emits ({@code 0} = a quiet source).</li>
 *   <li>{@code TotalEvents} — an optional lifetime cap. Uncapped (the default), the source is
 *       <b>replay-stable</b>: every tick emits the same number forever, which is what the perf
 *       tier requires of a reader. Capped, it is <b>finite</b> — it drains and then goes quiet,
 *       which is exactly the shape whose per-replay emission count varies and which the perf
 *       tier must reject loudly rather than benchmark.</li>
 *   <li>{@code ThrowOnTick} / {@code ThrowOnClose} — the failure paths.</li>
 * </ul>
 *
 * <p>The {@code Channel} interface is top-level, not nested: an OP jar may not ship inner classes,
 * and a reader's channel is no exception.</p>
 */
final class StubReaderJars {

    private StubReaderJars() {
    }

    /**
     * Compiles the stub against this module's own compiled classes — so its {@code WAEvent}
     * reference resolves to the same mock class the tests use — and packs it into a jar whose
     * manifest declares a {@code Striim-Service-Implementation}. The App class named there need
     * not exist; the harness only derives the core's package from it.
     */
    static File build(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("reader-src");
        Path classesDir = tempDir.resolve("reader-classes");
        Path pkgDir = srcDir.resolve("com/example/stubreader");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        Path channelFile = pkgDir.resolve("Channel.java");
        Files.writeString(channelFile, """
                package com.example.stubreader;

                import com.webaction.proc.events.WAEvent;

                public interface Channel {
                    void emit(WAEvent event) throws Exception;

                    void checkpoint(Object position);
                }
                """);

        Path processorFile = pkgDir.resolve("Processor.java");
        Files.writeString(processorFile, """
                package com.example.stubreader;

                import java.util.Map;

                import com.webaction.proc.events.WAEvent;

                public class Processor {
                    private final int perTick;
                    private final int totalEvents;
                    private final boolean throwOnTick;
                    private final boolean throwOnClose;
                    private int ticks;
                    private int emitted;

                    public Processor(Map<String, Object> props) {
                        this.perTick = Integer.parseInt(String.valueOf(props.getOrDefault("EventsPerTick", "1")));
                        // -1 = uncapped, i.e. a cyclic (replay-stable) source.
                        this.totalEvents = Integer.parseInt(String.valueOf(props.getOrDefault("TotalEvents", "-1")));
                        this.throwOnTick = "true".equals(props.get("ThrowOnTick"));
                        this.throwOnClose = "true".equals(props.get("ThrowOnClose"));
                    }

                    public void tick(Channel channel) throws Exception {
                        ticks++;
                        if (throwOnTick) {
                            throw new IllegalStateException("the source could not be read");
                        }
                        for (int i = 0; i < perTick; i++) {
                            if (totalEvents >= 0 && emitted >= totalEvents) {
                                break;
                            }
                            WAEvent event = new WAEvent();
                            event.putUserdata("tick", ticks);
                            channel.emit(event);
                            emitted++;
                        }
                        channel.checkpoint("after-tick-" + ticks);
                    }

                    public void close() {
                        if (throwOnClose) {
                            throw new IllegalStateException("closing the source failed too");
                        }
                    }
                }
                """);

        int compileResult = compiler.run(null, null, null,
                "-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString(),
                channelFile.toString(), processorFile.toString());
        if (compileResult != 0) {
            throw new IllegalStateException("Compiling the stub reader failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("stubreader.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation",
                "com.example.stubreader.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            for (String name : List.of("Channel.class", "Processor.class")) {
                String entryName = "com/example/stubreader/" + name;
                jarOut.putNextEntry(new JarEntry(entryName));
                Files.copy(classesDir.resolve(entryName), jarOut);
                jarOut.closeEntry();
            }
        }

        return jarPath.toFile();
    }
}
