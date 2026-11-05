package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
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

/**
 * {@link IntegrationProcessor#driveTarget} resumes by position after a restart: every event at or
 * below the writer's durable position on its path is dropped, as the platform's filter does.
 *
 * <p>The stub writer logs each received event as {@code id@position} and reports, by mode:
 * {@code eager} the last position received per path (a writer that checkpoints a position whose
 * siblings are still uncommitted), {@code complete} the highest position strictly below the last
 * received (only fully committed runs).</p>
 */
class TargetResumeByPositionTest {

    @TempDir
    Path tempDir;

    /** STG/LND pairs at P1, P2, then a lone P3; restart after the STG of P2. */
    private static final String PAIRS = "["
            + "{\"metadata\":{\"id\":\"s1\"},\"position\":1},"
            + "{\"metadata\":{\"id\":\"l1\"},\"position\":1},"
            + "{\"metadata\":{\"id\":\"s2\"},\"position\":2},"
            + "{\"metadata\":{\"id\":\"l2\"},\"position\":2},"
            + "{\"metadata\":{\"id\":\"s3\"},\"position\":3}]";

    private static final String PLAIN = "["
            + "{\"metadata\":{\"id\":\"e1\"}},{\"metadata\":{\"id\":\"e2\"}},"
            + "{\"metadata\":{\"id\":\"e3\"}},{\"metadata\":{\"id\":\"e4\"}},"
            + "{\"metadata\":{\"id\":\"e5\"}}]";

    private record Run(TargetReport report, List<String> log) {
    }

    private Run drive(String mode, String fixture, Integer... restartAfter) throws Exception {
        Path log = tempDir.resolve(mode + "-" + System.nanoTime() + ".log");
        TargetSpec spec = new TargetSpec();
        spec.restartAfter = List.of(restartAfter);
        TargetReport report = IntegrationProcessor.driveTarget(stubWriterJar(),
                Map.of("Mode", mode, "Log", log.toString()),
                WAEventJsonFactory.readEvents(fixture), null, null, spec,
                ScriptedPositions.read(fixture));
        return new Run(report, Files.exists(log) ? Files.readAllLines(log) : List.of());
    }

    @Test
    void aCheckpointAtTheSharedPositionDropsTheUncommittedSibling() throws Exception {
        // The defect the filter reproduces: durable P2 after only s2, so l2 is never delivered.
        Run run = drive("eager", PAIRS, 3);
        assertEquals(List.of("s1@1", "l1@1", "s2@2", "s3@3"), run.log());
        assertEquals(1, run.report().eventsDropped);
        assertEquals(0, run.report().eventsReplayed);
        assertEquals(4, run.report().eventsAccepted);
    }

    @Test
    void aCheckpointBelowTheSharedPositionReplaysTheWholeRun() throws Exception {
        // Durable P1: the platform replays every event at P2, the committed sibling s2 included.
        Run run = drive("complete", PAIRS, 3);
        assertEquals(List.of("s1@1", "l1@1", "s2@2", "s2@2", "l2@2", "s3@3"), run.log());
        assertEquals(0, run.report().eventsDropped);
        assertEquals(1, run.report().eventsReplayed);
        assertEquals("1", run.report().durablePositionBeforeRestart);
    }

    @Test
    void withoutPositionsTheResumeIsTheOldIndexResume() throws Exception {
        Run eager = drive("eager", PLAIN, 3);
        assertEquals(List.of("e1@1", "e2@2", "e3@3", "e4@4", "e5@5"), eager.log());
        assertEquals(0, eager.report().eventsReplayed);

        Run complete = drive("complete", PLAIN, 3);
        assertEquals(List.of("e1@1", "e2@2", "e3@3", "e3@3", "e4@4", "e5@5"), complete.log());
        assertEquals(1, complete.report().eventsReplayed);
        assertEquals(0, complete.report().eventsDropped);
    }

    @Test
    void twoRestartsInsideRuns() throws Exception {
        String fixture = "["
                + "{\"metadata\":{\"id\":\"a\"},\"position\":1},"
                + "{\"metadata\":{\"id\":\"b\"},\"position\":1},"
                + "{\"metadata\":{\"id\":\"c\"},\"position\":1},"
                + "{\"metadata\":{\"id\":\"d\"},\"position\":2},"
                + "{\"metadata\":{\"id\":\"e\"},\"position\":2},"
                + "{\"metadata\":{\"id\":\"f\"},\"position\":3}]";
        // First restart: nothing complete, the whole P1 run replays. Second: P1 complete, P2 replays.
        Run run = drive("complete", fixture, 2, 4);
        assertEquals(List.of("a@1", "b@1", "a@1", "b@1", "c@1", "d@2", "d@2", "e@2", "f@3"),
                run.log());
        assertEquals(2, run.report().restarts);
        assertEquals(3, run.report().eventsReplayed);
    }

    @Test
    void pathsAreFilteredIndependently() throws Exception {
        String fixture = "["
                + "{\"metadata\":{\"id\":\"a1\"},\"position\":{\"ordinal\":1,\"path\":\"a\"}},"
                + "{\"metadata\":{\"id\":\"b1\"},\"position\":{\"ordinal\":1,\"path\":\"b\"}},"
                + "{\"metadata\":{\"id\":\"a2\"},\"position\":{\"ordinal\":2,\"path\":\"a\"}},"
                + "{\"metadata\":{\"id\":\"b2\"},\"position\":{\"ordinal\":2,\"path\":\"b\"}}]";
        // Eager durable: a at 2, b at 1. Only b2 is above its path's checkpoint.
        Run run = drive("eager", fixture, 3);
        assertEquals(List.of("a1@1", "b1@1", "a2@2", "b2@2"), run.log());
        assertEquals(0, run.report().eventsReplayed);
        assertEquals(0, run.report().eventsDropped);
    }

    @Test
    void aCheckpointAheadOfTheStreamIsReported() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> drive("ahead", PLAIN, 2));
        assertTrue(e.getMessage().contains("ahead of the stream"), e.getMessage());
    }

    @Test
    void explicitPositionsOutsideATargetCaseAreRefused() throws Exception {
        Path input = tempDir.resolve("in.json");
        Files.writeString(input, PAIRS);
        IntegrationProcessor.Request request = new IntegrationProcessor.Request();
        request.opJar = "unused.jar";
        request.inputFile = input.toString();
        request.outputFile = tempDir.resolve("out.json").toString();
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.run(request));
        assertTrue(e.getMessage().contains("only a target: case"), e.getMessage());

        request.target = new TargetSpec();
        request.target.positions = Boolean.FALSE;
        assertThrows(IllegalArgumentException.class, () -> IntegrationProcessor.run(request));
    }

    private File stubWriterJar() throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-writer test");

        Path srcDir = tempDir.resolve("writer-src");
        Path classesDir = tempDir.resolve("writer-classes");
        Path pkgDir = srcDir.resolve("com/example/poswriter");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);
        Path jarPath = tempDir.resolve("poswriter.jar");
        if (Files.exists(jarPath)) {
            return jarPath.toFile();
        }

        Path writerFile = pkgDir.resolve("PosWriter.java");
        Files.writeString(writerFile, """
                package com.example.poswriter;

                import java.nio.file.Files;
                import java.nio.file.Paths;
                import java.nio.file.StandardOpenOption;
                import java.util.LinkedHashMap;
                import java.util.Map;

                import com.striim.testing.inttest.OrdinalPosition;
                import com.webaction.event.Event;
                import com.webaction.proc.RetriableWriter;
                import com.webaction.proc.events.WAEvent;
                import com.webaction.recovery.ImmutableStemma;
                import com.webaction.recovery.Path;
                import com.webaction.recovery.Position;
                import com.webaction.source.lib.prop.Property;
                import com.webaction.source.lib.prop.RetryPolicy;
                import com.webaction.uuid.UUID;

                public class PosWriter extends RetriableWriter {
                    // Survives restart(): the class, and so its statics, outlive the instance.
                    private static final Map<UUID, Path> DURABLE = new LinkedHashMap<>();
                    private static final Map<UUID, Long> LAST = new LinkedHashMap<>();
                    private String mode;
                    private String log;

                    @Override
                    public void init(Map<String, Object> properties, Map<String, Object> formatter,
                            UUID stream, String distributionID) throws Exception {
                        super.init(properties, formatter, stream, distributionID);
                        mode = (String) properties.get("Mode");
                        log = (String) properties.get("Log");
                    }

                    @Override
                    public void processEvent(int channel, Event event, ImmutableStemma position)
                            throws Exception {
                        long at = ((OrdinalPosition) position.getLowSourcePosition()).ordinal();
                        UUID component = position.getComponentID();
                        Files.writeString(Paths.get(log),
                                ((WAEvent) event).metadata.get("id") + "@" + at + "\\n",
                                StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                        if ("eager".equals(mode)) {
                            DURABLE.put(component, position.toPaths().get(0));
                        } else if ("complete".equals(mode)) {
                            Long last = LAST.put(component, at);
                            if (last != null && at > last) {
                                DURABLE.put(component, new Path(component,
                                        position.getDistributionID(), new OrdinalPosition(last)));
                            }
                        } else {
                            DURABLE.put(component, new Path(component,
                                    position.getDistributionID(), new OrdinalPosition(99)));
                        }
                    }

                    @Override
                    public void processEvent(int channel, Event event) {
                    }

                    @Override
                    public Position getEndpointCheckpoint() {
                        return DURABLE.isEmpty() ? null : new Position(DURABLE.values());
                    }

                    @Override public void cleanup() { }
                    @Override public boolean isDiscarded(Event event) { return false; }
                    @Override public RetryPolicy getRetryPolicy(Property property) { return null; }
                }
                """);

        int result = compiler.run(null, null, null,
                "-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString(), writerFile.toString());
        if (result != 0) {
            throw new IllegalStateException("Compiling the stub writer failed (exit " + result + ")");
        }

        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation",
                "com.example.poswriter.PosWriter");
        List<String> entries = new ArrayList<>();
        try (var walk = Files.walk(classesDir)) {
            walk.filter(Files::isRegularFile)
                    .forEach(f -> entries.add(classesDir.relativize(f).toString().replace('\\', '/')));
        }
        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            for (String entry : entries) {
                jarOut.putNextEntry(new JarEntry(entry));
                Files.copy(classesDir.resolve(entry), jarOut);
                jarOut.closeEntry();
            }
        }
        return jarPath.toFile();
    }
}
