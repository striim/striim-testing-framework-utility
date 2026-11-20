package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import org.junit.jupiter.api.Test;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * What a target perf iteration reports as evidence that the writer wrote something. A writer emits
 * nothing, so without these fields a writer that dropped every event reported a throughput and
 * passed; the Python runner fails an iteration on them (PERF_SPEC.md §4, §6).
 */
class PerformanceProcessorWriteEvidenceTest {

    /** Accepts every event and emits nothing; acknowledges what it accepted at each flush. */
    private static class FakeWriter implements EventDriver {
        private final boolean acknowledges;
        private final boolean acksAnything;
        private final String processed;
        private long accepted;
        private long acked;

        FakeWriter(boolean acknowledges, boolean acksAnything, String processed) {
            this.acknowledges = acknowledges;
            this.acksAnything = acksAnything;
            this.processed = processed;
        }

        @Override
        public List<WAEvent> processEvent(Event event) {
            accepted++;
            return List.of();
        }

        @Override
        public void endOfWindow() {
            if (acksAnything) {
                acked = accepted;
            }
        }

        @Override
        public boolean writes() {
            return true;
        }

        @Override
        public long acknowledgedEvents() {
            return acknowledges ? acked : -1;
        }

        @Override
        public String publishedProcessed() {
            return processed;
        }

        @Override
        public void close() {
        }
    }

    /** A processor: emits nothing here, and is not a writer. */
    private static final class Processor implements EventDriver {
        @Override
        public List<WAEvent> processEvent(Event event) {
            return List.of();
        }

        @Override
        public void close() {
        }
    }

    private static PerformanceProcessor.PerfRequest request(int warmupRuns, int runSize) {
        PerformanceProcessor.PerfRequest r = new PerformanceProcessor.PerfRequest();
        r.warmupRuns = warmupRuns;
        r.runSize = runSize;
        r.correctnessMode = "disabled";
        // A non-null source skips per-record materialization, so the templates can be placeholders.
        r.source = new SourceSpec();
        return r;
    }

    private static final List<Event> TWO_RECORDS = Arrays.asList(null, null);

    @Test
    void theAckCountIsTheMeasuredWindowsOwnNotTheWarmups() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new FakeWriter(true, true, "10"), result);

        assertNull(failure);
        assertEquals(6, result.measuredInputEvents);
        assertEquals(6L, result.measuredAckedEvents, "4 warmup acks are not the window's");
        assertEquals("10", result.publishedProcessed);
    }

    @Test
    void aWriterThatAcknowledgesNothingReportsZero() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(1, 3), TWO_RECORDS,
                new FakeWriter(true, false, null), result);

        assertNull(failure, "reported, not thrown: the runner decides the iteration");
        assertEquals(0L, result.measuredAckedEvents);
        assertNull(result.publishedProcessed);
    }

    @Test
    void aWriterWithoutTheAcknowledgeableMarkerReportsNoAckCount() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        PerformanceProcessor.runWarmupAndMeasured(request(0, 2), TWO_RECORDS,
                new FakeWriter(false, false, "4"), result);

        assertNull(result.measuredAckedEvents);
        assertEquals("4", result.publishedProcessed);
    }

    @Test
    void aMonitorReadThatThrowsFailsTheIteration() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(0, 2), TWO_RECORDS,
                new FakeWriter(true, true, null) {
                    @Override
                    public String publishedProcessed() {
                        throw new IllegalStateException("publishMonitorEvents blew up");
                    }
                }, result);

        assertNotNull(failure);
        assertTrue(result.errorMessage.contains("publishMonitorEvents blew up"), result.errorMessage);
    }

    // --- PROCESSED is cumulative: only its rise across the measured window is that window's ------

    /**
     * Publishes a cumulative PROCESSED, as AbstractWriterApp does, adding what it accepted at each
     * of its first {@code countedFlushes} flushes. Records the order of its calls.
     */
    private static class ProcessedWriter extends FakeWriter {
        private final int countedFlushes;
        private final List<String> calls = new ArrayList<>();
        private long pending;
        private long processedCount;
        private int flushes;

        ProcessedWriter(boolean acknowledges, boolean acksAnything, int countedFlushes) {
            super(acknowledges, acksAnything, null);
            this.countedFlushes = countedFlushes;
        }

        @Override
        public List<WAEvent> processEvent(Event event) {
            calls.add("process");
            pending++;
            return super.processEvent(event);
        }

        @Override
        public void endOfWindow() {
            calls.add("flush");
            super.endOfWindow();
            if (flushes++ < countedFlushes) {
                processedCount += pending;
            }
            pending = 0;
        }

        @Override
        public String publishedProcessed() {
            calls.add("read");
            return Long.toString(processedCount);
        }
    }

    @Test
    void processedWrittenOnlyDuringWarmupIsNotTheMeasuredWindowsEvidence() throws Exception {
        // 2 warmup replays x 2 records are written and counted at the warmup's flush; the measured
        // window's 6 events are accepted, flushed without error, and neither written nor acked.
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new ProcessedWriter(false, false, 1), result);

        assertNull(failure);
        assertEquals(6, result.measuredInputEvents);
        assertNull(result.measuredAckedEvents);
        assertEquals("4", result.publishedProcessed, "the cumulative value, warmup included");
        assertEquals(0L, result.measuredProcessed, "nothing was written in the measured window");
        // The result file carries it: the Python runner rejects measuredProcessed 0.
        assertTrue(new ObjectMapper().writeValueAsString(result).contains("\"measuredProcessed\":0"));
    }

    @Test
    void aWriterThatAcksDiscardedMeasuredEventsShowsNoProcessedRise() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new ProcessedWriter(true, true, 1), result);

        assertEquals(6L, result.measuredAckedEvents);
        assertEquals(0L, result.measuredProcessed);
    }

    @Test
    void anOrdinaryWriterReportsTheWindowsOwnProcessedRise() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new ProcessedWriter(true, true, Integer.MAX_VALUE), result);

        assertNull(failure);
        assertEquals(6L, result.measuredAckedEvents);
        assertEquals("10", result.publishedProcessed);
        assertEquals(6L, result.measuredProcessed, "4 warmup rows are not the window's");
    }

    @Test
    void processedIsReadAfterTheWarmupFlushBeforeTheWindowAndAfterItsFlush() {
        ProcessedWriter writer = new ProcessedWriter(false, false, Integer.MAX_VALUE);
        PerformanceProcessor.runWarmupAndMeasured(request(1, 1), TWO_RECORDS, writer, new PerformanceProcessor.PerfResult());

        assertEquals(List.of("process", "process", "flush", "read", "process", "process", "flush", "read"),
                writer.calls);
    }

    @Test
    void aWriterThatPublishesNoProcessedReportsNoProcessedRise() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(1, 2), TWO_RECORDS,
                new FakeWriter(true, true, null), result);

        assertNull(failure);
        assertNull(result.measuredProcessed, "the acknowledgment count is the evidence");
        assertEquals(4L, result.measuredAckedEvents);
    }

    @Test
    void processedFirstPublishedAfterTheWindowRisesFromZero() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(0, 2), TWO_RECORDS,
                new FakeWriter(false, false, null) {
                    private int reads;

                    @Override
                    public String publishedProcessed() {
                        return reads++ == 0 ? null : "4";
                    }
                }, result);

        assertNull(failure);
        assertEquals(4L, result.measuredProcessed);
    }

    @Test
    void aProcessedReadBeforeTheWindowThatThrowsFailsTheIterationUnmeasured() {
        ProcessedWriter writer = new ProcessedWriter(false, false, Integer.MAX_VALUE) {
            @Override
            public String publishedProcessed() {
                throw new IllegalStateException("publishMonitorEvents blew up early");
            }
        };
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(1, 2), TWO_RECORDS, writer, result);

        assertNotNull(failure);
        assertTrue(result.errorMessage.contains("publishMonitorEvents blew up early"), result.errorMessage);
        assertEquals(0, result.measuredInputEvents, "the window did not run");
        assertEquals(List.of("process", "process", "flush"), writer.calls);
    }

    @Test
    void aProcessedValueThatIsNotANumberFailsTheIteration() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(1, 2), TWO_RECORDS,
                new FakeWriter(false, false, "1,024"), result);

        assertNotNull(failure);
        assertTrue(result.errorMessage.contains("PROCESSED"), result.errorMessage);
        assertTrue(result.errorMessage.contains("1,024"), result.errorMessage);
        assertNull(result.measuredProcessed);
    }

    @Test
    void processedThatDisappearsAfterTheWindowFailsTheIteration() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(1, 2), TWO_RECORDS,
                new FakeWriter(false, false, null) {
                    private int reads;

                    @Override
                    public String publishedProcessed() {
                        return reads++ == 0 ? "4" : null;
                    }
                }, result);

        assertNotNull(failure);
        assertTrue(result.errorMessage.contains("PROCESSED"), result.errorMessage);
        assertNull(result.measuredProcessed);
    }

    @Test
    void aProcessorReportsNoWriterEvidence() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        PerformanceProcessor.runWarmupAndMeasured(request(1, 2), TWO_RECORDS, new Processor(), result);

        assertNull(result.measuredAckedEvents);
        assertNull(result.publishedProcessed);
        assertNull(result.measuredProcessed);
    }
}
