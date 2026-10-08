package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * replaying a READER through the perf harness.
 *
 * <p><b>The question this slice existed to answer.</b> PERF_SPEC.md §6 makes emission-count
 * stability a mandatory check — "an operator whose emitted-record count varies across identical
 * replays is out of scope for this framework" — and that was recorded as possibly fatal,
 * since a reader is the obvious op whose count varies. It is not fatal, and these tests are why:
 * the check does not exclude readers, it <b>defines the contract a reader's source seam must
 * meet</b>, and it enforces that contract loudly and for free.</p>
 *
 * <p>A reader's replay is {@code source.max_ticks} ticks rather than one pass over an input
 * fixture. A <b>replay-stable</b> (cyclic) source emits the same count every replay and benchmarks
 * exactly as an in-stream op does. A <b>finite</b> source drains during replay 0 and goes quiet,
 * and the existing stability check catches it — naming the replay where it deviated — instead of
 * reporting a meaningless throughput number.</p>
 */
class PerformanceProcessorSourceTest {

    @Test
    void aReplayStableSourceBenchmarksLikeAnyOtherOp(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        // Uncapped: every tick emits 2 events forever, so every replay of 4 ticks emits 8.
        PerformanceProcessor.PerfRequest request = readerRequest(tempDir, readerJar,
                Map.of("EventsPerTick", "2"), 4);
        request.warmupRuns = 1;
        request.runSize = 3;

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        // "Input events" for a reader are TICKS -- the drive steps -- which is what makes the
        // in-stream replay machinery apply unchanged.
        assertEquals(4, result.warmupInputEvents);
        assertEquals(8, result.warmupOutputEvents);
        assertEquals(12, result.measuredInputEvents);
        assertEquals(24, result.measuredOutputEvents);

        assertEquals(8, result.comparedReplayOutputEvents);
        assertTrue(result.perReplayOutputEventsStable);
        assertNull(result.firstUnstableReplayIndex);
        assertNull(result.errorMessage);
    }

    @Test
    void aFiniteSourceIsRejectedByTheStabilityCheckRatherThanBenchmarked(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        // Capped at 10 events for the reader's whole lifetime. Warmup (4 ticks x 2) takes 8, so
        // measured replay 0 gets the last 2 and every replay after it gets nothing -- the shape
        // whose throughput number would be meaningless.
        PerformanceProcessor.PerfRequest request = readerRequest(tempDir, readerJar,
                Map.of("EventsPerTick", "2", "TotalEvents", "10"), 4);
        request.warmupRuns = 1;
        request.runSize = 3;

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertEquals(2, result.comparedReplayOutputEvents);
        assertFalse(result.perReplayOutputEventsStable,
                "a source that drains must not pass the stability check");
        // Replay 1 is the first to deviate (0 emitted vs replay 0's 2), and it is named rather
        // than merely flagged -- the author needs to know WHERE the source ran dry.
        assertEquals(1, result.firstUnstableReplayIndex);
        // Not an error: the window still ran to completion, so duration stays meaningful and the
        // Python runner is the one that fails the iteration on the unstable flag (PERF_SPEC §6).
        assertNull(result.errorMessage);
    }

    @Test
    void aQuietSourceIsStableAtZeroRatherThanUnstable(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        PerformanceProcessor.PerfRequest request = readerRequest(tempDir, readerJar,
                Map.of("EventsPerTick", "0"), 3);
        request.warmupRuns = 0;
        request.runSize = 2;

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        // Emitting nothing every replay IS stable -- the check is about variance, not volume.
        assertEquals(6, result.measuredInputEvents);
        assertEquals(0, result.measuredOutputEvents);
        assertEquals(0, result.comparedReplayOutputEvents);
        assertTrue(result.perReplayOutputEventsStable);
    }

    @Test
    void aSourcePerfRequestNeedsNoInputFile(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        PerformanceProcessor.PerfRequest request = readerRequest(tempDir, readerJar,
                Map.of("EventsPerTick", "1"), 2);
        request.warmupRuns = 0;
        request.runSize = 1;
        request.inputFile = null;   // a reader has no fixture, and run() must not demand one

        PerformanceProcessor.PerfResult result = PerformanceProcessor.run(request);

        assertEquals(2, result.measuredOutputEvents);
    }

    @Test
    void anInputFileAlongsideASourceIsRejectedRatherThanIgnored(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);
        Path inputFile = tempDir.resolve("input.json");
        Files.writeString(inputFile, "[]");

        PerformanceProcessor.PerfRequest request = readerRequest(tempDir, readerJar,
                Map.of("EventsPerTick", "1"), 2);
        request.warmupRuns = 0;
        request.runSize = 1;
        request.inputFile = inputFile.toString();

        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> PerformanceProcessor.run(request));

        assertTrue(e.getMessage().contains("inputFile has no meaning"), e.getMessage());
    }

    private static PerformanceProcessor.PerfRequest readerRequest(Path tempDir, File readerJar,
            Map<String, Object> properties, int maxTicks) {
        PerformanceProcessor.PerfRequest request = new PerformanceProcessor.PerfRequest();
        request.opJar = readerJar.getAbsolutePath();
        request.properties = properties;
        request.resultFile = tempDir.resolve("result.json").toString();
        request.correctnessMode = "disabled";
        request.source = new SourceSpec();
        request.source.maxTicks = maxTicks;
        return request;
    }
}
