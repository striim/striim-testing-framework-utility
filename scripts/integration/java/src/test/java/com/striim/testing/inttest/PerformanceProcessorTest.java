package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
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

import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Proves {@link PerformanceProcessor#run} implements the warmup/measured-window
 * protocol (PERF_SPEC.md §4-§6) against a stub op compiled on the fly (mirroring
 * {@link IntegrationProcessorLifecycleTest}/{@link IntegrationProcessorTypesTest}'s
 * pattern): warmup/measured counts, correctness-mode record retention (full/sampled/
 * disabled), emission-count stability detection, and error capture (both warmup and
 * measured phases) with the result file always written before a failure propagates.
 *
 * <p>
 * The stub {@code Processor} (single-{@code Map}-arg constructor, no
 * {@code BuiltInFuncs}/{@code TypeResolver} needed) reads two properties: {@code Mode}
 * ({@code "passthrough"} default, or {@code "unstable"} to emit an alternating
 * record count so the stability check has something to catch) and
 * {@code ThrowOnCall} (a 1-based {@code processEvent} call count to throw on, or
 * {@code -1} to never throw). A {@code static int callCount} gives each test a fresh
 * counter: {@link OperatorCore#build} loads the op jar into a brand-new
 * {@code URLClassLoader} per call, so static state never leaks between tests even
 * within one JVM.
 */
class PerformanceProcessorTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static final String TWO_RECORDS = """
            [
              {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [true]}},
              {"metadata": {"TableName": "T"}, "data": {"values": ["b"], "present": [true]}}
            ]
            """;

    private static final String ONE_RECORD = """
            [
              {"metadata": {"TableName": "T"}, "data": {"values": ["a"], "present": [true]}}
            ]
            """;

    @Test
    void warmupAndMeasuredCountsAreAccurate(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, TWO_RECORDS, Map.of());
        request.warmupRuns = 1;
        request.runSize = 3;
        request.correctnessMode = "disabled";

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertEquals(2, result.warmupInputEvents);
        assertEquals(2, result.warmupOutputEvents);
        assertEquals(6, result.measuredInputEvents);
        assertEquals(6, result.measuredOutputEvents);
        assertEquals(2, result.comparedReplayOutputEvents);
        assertTrue(result.perReplayOutputEventsStable);
        assertNull(result.firstUnstableReplayIndex);
        assertNull(result.errorMessage);
        assertTrue(result.measuredDurationNanos >= 0);
        assertTrue(result.measuredEndEpochMillis >= result.measuredStartEpochMillis);

        // The result file on disk must match what run() returned in-process.
        PerformanceProcessor.PerfResult onDisk = MAPPER.readValue(new File(request.resultFile), PerformanceProcessor.PerfResult.class);
        assertEquals(result.measuredInputEvents, onDisk.measuredInputEvents);
        assertEquals(result.measuredOutputEvents, onDisk.measuredOutputEvents);
    }

    @Test
    void fullModeRetainsEveryComparedRecord(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, TWO_RECORDS, Map.of());
        request.warmupRuns = 0;
        request.runSize = 2;
        request.correctnessMode = "full";

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertEquals(2, result.comparedRecords.size());
        assertEquals("a", result.comparedRecords.get(0).get("data").get("values").get(0).asText());
        assertEquals("b", result.comparedRecords.get(1).get("data").get("values").get(0).asText());
    }

    @Test
    void sampledModeRetainsOnlyRequestedAbsoluteIndices(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, TWO_RECORDS, Map.of());
        request.warmupRuns = 0;
        request.runSize = 2;
        request.correctnessMode = "sampled";
        request.sampleIndices = List.of(1);

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertEquals(1, result.comparedRecords.size());
        assertEquals("b", result.comparedRecords.get(0).get("data").get("values").get(0).asText());
    }

    @Test
    void disabledModeRetainsNoComparedRecords(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, TWO_RECORDS, Map.of());
        request.warmupRuns = 0;
        request.runSize = 2;
        request.correctnessMode = "disabled";

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertTrue(result.comparedRecords.isEmpty());
    }

    @Test
    void detectsUnstableEmissionCountsWithoutAbortingTheWindow(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, ONE_RECORD, Map.of("Mode", "unstable"));
        request.warmupRuns = 0;
        request.runSize = 3;
        request.correctnessMode = "disabled";

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        // callCount 1 (replay0, odd) -> 1 record; callCount 2 (replay1, even) -> 2 records
        // (mismatch, first deviation); callCount 3 (replay2, odd) -> 1 record (not re-flagged).
        assertEquals(1, result.comparedReplayOutputEvents);
        assertFalse(result.perReplayOutputEventsStable);
        assertEquals(1, result.firstUnstableReplayIndex);
        assertEquals(3, result.measuredInputEvents);
        assertEquals(4, result.measuredOutputEvents);
        // The window ran to completion despite the mismatch -- not aborted early.
        assertNull(result.errorMessage);
    }

    @Test
    void measuredWindowErrorIsCapturedAndResultFileStillWritten(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, ONE_RECORD, Map.of("ThrowOnCall", "2"));
        request.warmupRuns = 0;
        request.runSize = 2;
        request.correctnessMode = "disabled";

        assertThrows(Exception.class, () -> PerformanceProcessor.run(request));

        assertTrue(Files.exists(Path.of(request.resultFile)), "result file must be written even after a measured-window error");
        PerformanceProcessor.PerfResult onDisk = MAPPER.readValue(new File(request.resultFile), PerformanceProcessor.PerfResult.class);
        assertNotNull(onDisk.errorMessage);
        assertEquals(1, onDisk.errorReplayIndex); // second processEvent call -> measured replay index 1
        assertEquals(0, onDisk.errorRecordIndex);
        assertEquals(1, onDisk.measuredInputEvents); // only replay 0's single call succeeded before the throw
    }

    @Test
    void warmupErrorIsCapturedWithNegativeReplayIndex(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubOpJar(tempDir);
        PerformanceProcessor.PerfRequest request = baseRequest(tempDir, stubJar, ONE_RECORD, Map.of("ThrowOnCall", "1"));
        request.warmupRuns = 1;
        request.runSize = 1;
        request.correctnessMode = "disabled";

        assertThrows(Exception.class, () -> PerformanceProcessor.run(request));

        PerformanceProcessor.PerfResult onDisk = MAPPER.readValue(new File(request.resultFile), PerformanceProcessor.PerfResult.class);
        assertNotNull(onDisk.errorMessage);
        assertEquals(-1, onDisk.errorReplayIndex); // warmup replay 0 -> -(0+1)
        assertEquals(0, onDisk.errorRecordIndex);
        assertEquals(0, onDisk.warmupInputEvents); // failed before the count was incremented
        assertEquals(0, onDisk.measuredInputEvents); // never reached the measured window
    }

    private static PerformanceProcessor.PerfRequest baseRequest(Path tempDir, File stubJar, String inputJson, Map<String, Object> properties) throws IOException {
        Path inputFile = tempDir.resolve("perf-input-" + System.nanoTime() + ".json");
        Files.writeString(inputFile, inputJson);

        PerformanceProcessor.PerfRequest request = new PerformanceProcessor.PerfRequest();
        request.opJar = stubJar.getAbsolutePath();
        request.properties = new HashMap<>(properties);
        request.inputFile = inputFile.toString();
        request.resultFile = tempDir.resolve("perf-result-" + System.nanoTime() + ".json").toString();
        return request;
    }

    /**
     * Compiles the flexible passthrough/unstable/throwing stub op described in the
     * class javadoc, against this module's own compiled classes (so its
     * {@code WAEvent} reference resolves to the same mock class this test uses), and
     * packs it into a jar whose manifest declares a {@code Striim-Service-Implementation}
     * (the App class named there need not actually exist; only the core's package is
     * derived from it).
     */
    private static File buildStubOpJar(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("src-" + System.nanoTime());
        Path classesDir = tempDir.resolve("classes-" + System.nanoTime());
        Path pkgDir = srcDir.resolve("com/example/perfstubop");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        String source = """
                package com.example.perfstubop;

                import java.util.ArrayList;
                import java.util.List;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;

                public class Processor {
                    private static int callCount = 0;

                    private final String mode;
                    private final int throwOnCall;

                    public Processor(Map<String, Object> props) {
                        this.mode = String.valueOf(props.getOrDefault("Mode", "passthrough"));
                        this.throwOnCall = Integer.parseInt(String.valueOf(props.getOrDefault("ThrowOnCall", "-1")));
                    }

                    public List<WAEvent> processEvent(WAEvent event) {
                        callCount++;
                        if (callCount == throwOnCall) {
                            throw new RuntimeException("stub boom at call " + callCount);
                        }
                        if ("unstable".equals(mode)) {
                            int n = (callCount % 2 == 1) ? 1 : 2;
                            List<WAEvent> out = new ArrayList<>();
                            for (int i = 0; i < n; i++) {
                                out.add(WAEvent.makeCopy(event));
                            }
                            return out;
                        }
                        WAEvent copy = WAEvent.makeCopy(event);
                        copy.putUserdata("callCount", callCount);
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
            throw new IllegalStateException("Compiling stub perf op Processor.java failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("perfstubop-" + System.nanoTime() + ".jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation", "com.example.perfstubop.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            Path classFile = classesDir.resolve("com/example/perfstubop/Processor.class");
            String entryName = "com/example/perfstubop/Processor.class";
            jarOut.putNextEntry(new JarEntry(entryName));
            Files.copy(classFile, jarOut);
            jarOut.closeEntry();
        }

        return jarPath.toFile();
    }
}
