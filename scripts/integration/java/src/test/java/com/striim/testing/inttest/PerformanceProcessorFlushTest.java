package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

import java.util.Arrays;
import java.util.List;

import org.junit.jupiter.api.Test;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * A writer's flush ({@code endOfWindow()}) failing, in each window. A flush belongs to the window,
 * not to a record: the failure is attributed to the window's last replay with no record index,
 * and returned so the process exits nonzero (PERF_SPEC.md §4).
 */
class PerformanceProcessorFlushTest {

    /** Accepts every event, emits nothing, and fails the Nth endOfWindow() call. */
    private static final class FailingFlushDriver implements EventDriver {
        private final int failOnFlush;
        private int flushes;

        FailingFlushDriver(int failOnFlush) {
            this.failOnFlush = failOnFlush;
        }

        @Override
        public List<WAEvent> processEvent(Event event) {
            return List.of();
        }

        @Override
        public void endOfWindow() throws Exception {
            if (++flushes == failOnFlush) {
                throw new IllegalStateException("flush " + flushes + " failed");
            }
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
    void measuredWindowFlushFailureFailsTheIterationWithTheLastReplayAndNoRecord() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        // warmup flush is call 1, measured flush is call 2
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new FailingFlushDriver(2), result);

        assertNotNull(failure, "returned, so run() throws and the process exits nonzero");
        assertNotNull(result.errorMessage);
        assertEquals(2, result.errorReplayIndex); // runSize 3 -> last measured replay is 2
        assertNull(result.errorRecordIndex);
        assertEquals(6, result.measuredInputEvents);
    }

    @Test
    void warmupFlushFailureIsTheLastWarmupReplayUnderTheMinusWPlusOneRule() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(2, 3), TWO_RECORDS,
                new FailingFlushDriver(1), result);

        assertNotNull(failure);
        assertEquals(-2, result.errorReplayIndex); // warmup replay w=1 is -(w+1) = -2
        assertNull(result.errorRecordIndex);
        assertEquals(4, result.warmupInputEvents);
        assertEquals(0, result.measuredInputEvents); // never reached the measured window
    }

    @Test
    void noWarmupReplaysMeansNoWarmupFlush() {
        PerformanceProcessor.PerfResult result = new PerformanceProcessor.PerfResult();
        // With warmupRuns 0 the only flush is the measured one: failing call 1 must be it.
        Exception failure = PerformanceProcessor.runWarmupAndMeasured(request(0, 1), TWO_RECORDS,
                new FailingFlushDriver(1), result);

        assertNotNull(failure);
        assertEquals(0, result.errorReplayIndex);
        assertEquals(2, result.measuredInputEvents);
    }
}
