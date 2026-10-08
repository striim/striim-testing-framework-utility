package com.striim.testing.inttest;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.webaction.event.Event;
import com.webaction.proc.events.AvroEvent;
import com.webaction.proc.events.WAEvent;

/**
 * The performance-mode driver (docs/internals/PERF_SPEC.md §4-§6). Loads the
 * performance input file once into a template list, builds and starts the operator
 * core via the same {@link OperatorCore} {@link IntegrationProcessor} uses, runs
 * {@code warmupRuns} untimed complete replays, then a timed measured window of
 * {@code runSize} complete replays, and reports counts/timing/correctness-sample data
 * for the Python runner to aggregate, validate, and report (PERF_SPEC.md §7-§11).
 *
 * <p>
 * <b>Replay mechanism (PERF_SPEC.md §4, deliberate).</b> Each replay materializes a
 * fresh {@link WAEvent} per template via {@link WAEvent#makeCopy} — never reusing a
 * mutated instance across replays. Materialization happens <em>inside</em> the
 * measured window: production also allocates a fresh event per record, so this cost
 * belongs to the measurement. Parsing the input file happens once, before either
 * phase.
 *
 * <p>
 * <b>Compared replay (PERF_SPEC.md §6).</b> Replay 0 of the measured window is the
 * "compared replay". Its emitted records are retained per {@code correctnessMode}:
 * all of them ({@code full}), only those at {@code sampleIndices} ({@code sampled} —
 * absolute output-record indices, precomputed by the Python runner from {@code
 * len(expected)} and passed in, since this driver never reads {@code expected}
 * itself), or none ({@code disabled}). Every other replay's emitted-record count is
 * compared against replay 0's; the first mismatch is recorded but does not abort the
 * loop, so {@code measuredDurationNanos} always reflects the full configured window.
 */
public final class PerformanceProcessor {

    private PerformanceProcessor() {
    }

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** The on-disk performance request contract (PERF_SPEC.md §4). */
    public static final class PerfRequest {
        public String opJar;
        public Map<String, Object> properties;
        public String inputFile;
        public String resultFile;
        public Map<String, Object> types;
        public String namespace;
        public String sourceName;
        public List<String> passwordProperties;
        public int runSize;
        public int warmupRuns;
        /** {@code full}, {@code sampled}, or {@code disabled} (PERF_SPEC.md §6). */
        public String correctnessMode;
        /**
         * Absolute output-record indices (within replay 0's emitted stream) to retain;
         * only consulted when {@code correctnessMode} is {@code "sampled"}. Precomputed
         * by the Python runner from {@code len(expected)} — this driver never opens
         * {@code expected} itself.
         */
        public List<Integer> sampleIndices;
        /**
         * Optional {@code udf:} block (docs/INTEGRATION-TESTS.md). When present, {@code
         * opJar} is driven as a bare UDF pipeline ({@link UdfCore}) instead of as an
         * OpenProcessor {@code Processor} ({@link OperatorCore}).
         */
        public UdfSpec udf;
        /**
         * Optional {@code source:} block (PERF_SPEC.md §3). When present, {@code opJar} is
         * replayed as a READER: one replay is {@code source.maxTicks} ticks rather than one pass
         * over {@code inputFile}, which a reader has none of.
         */
        public SourceSpec source;

        /**
         * Optional {@code target:} block. When present the case drives a WRITER: events are fed
         * with {@code accept}, nothing is emitted, and the window ends in a {@code flush} inside
         * the timed region — see {@link TargetPerfCore} and status §64.3.
         */
        public TargetSpec target;
    }

    /** The on-disk performance result contract (PERF_SPEC.md §4). */
    public static final class PerfResult {
        public long measuredStartEpochMillis;
        public long measuredEndEpochMillis;
        public long measuredDurationNanos;
        public long warmupInputEvents;
        public long warmupOutputEvents;
        public long measuredInputEvents;
        public long measuredOutputEvents;
        public long comparedReplayOutputEvents;
        public boolean perReplayOutputEventsStable = true;
        /**
         * First measured replay index (0-based) whose emitted-record count differed
         * from replay 0's; {@code null} while {@link #perReplayOutputEventsStable} is
         * {@code true}.
         */
        public Integer firstUnstableReplayIndex;
        /** WAEvent-JSON of the retained compared-replay records (§6: all/sampled/none by correctness mode). */
        public List<ObjectNode> comparedRecords = new ArrayList<>();
        public String errorMessage;
        /**
         * The replay at which {@link #errorMessage} occurred. Negative during warmup
         * (warmup replay {@code w} -&gt; {@code -(w+1)}, so index 0 is distinguishable
         * from "no error"/measured index 0); non-negative during the measured window.
         * {@code null} when no error occurred.
         */
        public Integer errorReplayIndex;
        /** The record index (within one replay) at which {@link #errorMessage} occurred. */
        public Integer errorRecordIndex;
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            System.err.println("usage: PerformanceProcessor <request.json>");
            System.exit(2);
            return;
        }
        try {
            PerfRequest request = MAPPER.readValue(new File(args[0]), PerfRequest.class);
            run(request);
        } catch (Exception e) {
            System.err.println("PerformanceProcessor failed: " + e);
            e.printStackTrace();
            System.exit(1);
        }
    }

    /**
     * Runs the full warmup+measured protocol and writes {@code request.resultFile}
     * unconditionally — even after an operator error, so the Python runner gets
     * structured detail per §4 — then rethrows on error so the process exits nonzero
     * (mirroring {@link IntegrationProcessor#main}'s catch-and-exit(1) pattern).
     */
    public static PerfResult run(PerfRequest request) throws Exception {
        Objects.requireNonNull(request, "request");
        Objects.requireNonNull(request.opJar, "request.opJar is required");
        if (request.source == null) {
            Objects.requireNonNull(request.inputFile, "request.inputFile is required");
        }
        Objects.requireNonNull(request.resultFile, "request.resultFile is required");
        Objects.requireNonNull(request.correctnessMode, "request.correctnessMode is required");
        IntegrationProcessor.requireNoPropertiesForUdf(request.udf, request.properties, request.passwordProperties);

        // A reader has no input fixture, so ONE REPLAY IS `maxTicks` TICKS: the templates list
        // becomes that many blank tick triggers and every loop below -- warmup, the measured
        // window, sampling, and the emission-count stability check -- runs unchanged. That last
        // one is the point: it is exactly the check that enforces a reader's source seam being
        // REPLAY-STABLE, rather than a barrier to measuring one. See PERF_SPEC.md §6.
        List<Event> templates = request.source == null
                ? WAEventJsonFactory.readInputEvents(Files.readString(Path.of(request.inputFile)))
                : tickTriggers(request.source);
        PerfResult result = new PerfResult();

        // OperatorCore.build stamps typeUUID on `templates` itself (from request.types);
        // WAEvent.makeCopy below carries that stamp into every per-replay materialization.
        // (UdfCore needs no such stamping -- it resolves columns via BuiltInFunc.getIndexOfColumn
        // off metadata.TableName directly, not typeUUID. SourceCore needs none either -- a reader
        // MINTS its events rather than being fed them.)
        EventDriver core;
        if (request.source != null) {
            IntegrationProcessor.requireCoherentSource(request.source, request.udf, List.of());
            if (request.inputFile != null) {
                // The Python loader already rejects `performance.input` on a source: case; repeat
                // it here so a hand-built request does not get its fixture silently ignored.
                throw new IllegalArgumentException(
                        "a 'source:' perf case replays ticks, so inputFile has no meaning and would"
                                + " be silently ignored, but one was supplied: " + request.inputFile);
            }
            core = SourceCore.build(new File(request.opJar), request.properties, request.types,
                    request.passwordProperties, request.namespace, request.sourceName);
        } else if (request.target != null) {
            // A target is FED events and emits none, so it shares the replay machinery and differs
            // only in what a window costs -- which is the flush, not the accepts.
            core = TargetPerfCore.build(new File(request.opJar), request.properties, request.types,
                    request.passwordProperties, request.target.distributionId,
                    IntegrationProcessor.waEventsOnly(templates, "a target: perf case"));
        } else if (request.udf == null) {
            core = OperatorCore.build(new File(request.opJar), request.properties, request.types, templates,
                    request.passwordProperties, request.namespace, request.sourceName);
        } else {
            core = UdfCore.build(new File(request.opJar), request.udf, request.types);
        }

        Exception failure = runWarmupAndMeasured(request, templates, core, result);

        try {
            core.close();
        } catch (Exception e) {
            if (failure == null) {
                failure = e;
                result.errorMessage = describeError(e);
            }
        }

        Files.writeString(Path.of(request.resultFile), MAPPER.writeValueAsString(result));

        if (failure != null) {
            throw failure;
        }
        return result;
    }

    /**
     * One replay's worth of tick triggers. They carry no data: {@link SourceCore} ignores the
     * event it is handed, so these exist only to make one {@code processEvent} call happen per
     * tick and let the reader reuse the in-stream replay machinery verbatim.
     */
    private static List<Event> tickTriggers(SourceSpec source) {
        List<Event> triggers = new ArrayList<>(source.maxTicks);
        for (int i = 0; i < source.maxTicks; i++) {
            triggers.add(new WAEvent());
        }
        return triggers;
    }

    /**
     * Executes warmup then the measured window, mutating {@code result} in place and
     * returning the failing exception (or {@code null} on success) rather than
     * throwing directly, so {@link #run} can still write the result file before
     * propagating it.
     */
    /**
     * A fresh instance of one input template, for the per-record materialization §4 puts inside
     * the measured window.
     *
     * <p>Dispatches on the event KIND rather than assuming {@code WAEvent}: an operator whose
     * input is an {@code AvroEvent} needs that class's own copy, and a silent fall-through to
     * "reuse the template" would quietly measure a different thing than every other case — one
     * allocation per replay instead of one per record.</p>
     */
    private static Event materialize(Event template) {
        if (template instanceof WAEvent waEvent) {
            return WAEvent.makeCopy(waEvent);
        }
        if (template instanceof AvroEvent avroEvent) {
            return AvroEvent.makeCopy(avroEvent);
        }
        throw new IllegalStateException("no per-record copy is defined for input event kind "
                + (template == null ? "null" : template.getClass().getName())
                + "; add one here rather than reusing the template, which would measure a"
                + " different allocation profile than every other case");
    }

    // Package-private so a test can drive it with a fake EventDriver (a writer whose flush fails).
    static Exception runWarmupAndMeasured(PerfRequest request, List<Event> templates, EventDriver core, PerfResult result) {
        // §4 puts per-record materialization INSIDE the measured window because production also
        // allocates a fresh event per record. A reader has no input event at all -- its trigger is
        // a harness artifact SourceCore ignores -- so copying one per tick would time an
        // allocation with no production counterpart and bias the number down. Reusing the instance
        // is safe precisely because it is never read.
        boolean materializePerRecord = request.source == null;
        // --- Warmup: untimed complete replays, output counted and discarded (PERF_SPEC.md §5). ---
        for (int replay = 0; replay < request.warmupRuns; replay++) {
            for (int recordIndex = 0; recordIndex < templates.size(); recordIndex++) {
                Event instance = materializePerRecord
                        ? materialize(templates.get(recordIndex))
                        : templates.get(recordIndex);
                List<WAEvent> emitted;
                try {
                    emitted = core.processEvent(instance);
                } catch (Exception e) {
                    result.errorMessage = describeError(e);
                    result.errorReplayIndex = -(replay + 1);
                    result.errorRecordIndex = recordIndex;
                    return e;
                }
                result.warmupInputEvents++;
                result.warmupOutputEvents += emitted.size();
            }
        }

        // The warmup's own window must be closed, or its accumulated events would still be sitting
        // in the writer when the measured window starts and would be flushed -- and TIMED -- as
        // part of it. A no-op for every other driver.
        //
        // Reported the same way a warmup PROCESS failure is, with a negative replay index, rather
        // than thrown: the result file is written either way, and a warmup that failed to flush is
        // the same class of problem as one that failed to process. A flush belongs to the window,
        // not to a record: it is attributed to the window's LAST replay (warmup replay w is
        // -(w+1), so the last is -warmupRuns) with no record index. With no warmup replays there
        // is nothing to close.
        if (request.warmupRuns > 0) {
            try {
                core.endOfWindow();
            } catch (Exception e) {
                result.errorMessage = describeError(e);
                result.errorReplayIndex = -request.warmupRuns;
                result.errorRecordIndex = null;
                return e;
            }
        }

        // --- Measured window: materialization happens INSIDE the window (§4, deliberate). ---
        Set<Integer> sampleIndexSet = request.sampleIndices == null ? null : new HashSet<>(request.sampleIndices);
        long replay0OutputIndex = 0;
        long comparedReplayOutputEvents = 0;
        Exception failure = null;
        // Retained as raw WAEvent references during the timed window and converted to
        // ObjectNode only AFTER it closes (below) -- §5 excludes "result serialization"
        // from the measured interval, and building the ObjectNode tree per retained
        // record is real Jackson work, not a bookkeeping increment. Deferring this is
        // safe: IntegrationProcessor's own drive() already accumulates every emitted
        // WAEvent across the whole input and serializes it only after processing
        // completes, so retaining a returned WAEvent past the call that produced it is
        // an already-established assumption of this framework, not a new one here.
        List<WAEvent> retainedReplay0 = new ArrayList<>();

        long startNanos = System.nanoTime();
        long startEpoch = System.currentTimeMillis();

        outer:
        for (int replayIndex = 0; replayIndex < request.runSize; replayIndex++) {
            long thisReplayOutput = 0;
            for (int recordIndex = 0; recordIndex < templates.size(); recordIndex++) {
                Event instance = materializePerRecord
                        ? materialize(templates.get(recordIndex))
                        : templates.get(recordIndex);
                List<WAEvent> emitted;
                try {
                    emitted = core.processEvent(instance);
                } catch (Exception e) {
                    result.errorMessage = describeError(e);
                    result.errorReplayIndex = replayIndex;
                    result.errorRecordIndex = recordIndex;
                    failure = e;
                    break outer;
                }
                result.measuredInputEvents++;
                result.measuredOutputEvents += emitted.size();
                thisReplayOutput += emitted.size();
                if (replayIndex == 0) {
                    for (WAEvent out : emitted) {
                        if (shouldRetain(request.correctnessMode, sampleIndexSet, replay0OutputIndex)) {
                            retainedReplay0.add(out);
                        }
                        replay0OutputIndex++;
                    }
                }
            }
            if (replayIndex == 0) {
                comparedReplayOutputEvents = thisReplayOutput;
            } else if (thisReplayOutput != comparedReplayOutputEvents && result.perReplayOutputEventsStable) {
                // First deviation only (docs/INTEGRATION-TESTS.md) -- do not abort; duration must reflect the
                // full configured window regardless of a correctness mismatch.
                result.perReplayOutputEventsStable = false;
                result.firstUnstableReplayIndex = replayIndex;
            }
        }

        // ⚠ INSIDE the timed region, and that is the whole point for a target: accept() only
        // accumulates, so a window measured without its flush times the fold and nothing about the
        // database. A no-op for every other driver.
        // A failed flush is a failed iteration like a failed process: attributed to the window's
        // last replay with no record index, and returned so the process exits nonzero.
        if (result.errorMessage == null) {
            try {
                core.endOfWindow();
            } catch (Exception e) {
                result.errorMessage = describeError(e);
                result.errorReplayIndex = request.runSize - 1;
                result.errorRecordIndex = null;
                failure = e;
            }
        }

        long endNanos = System.nanoTime();
        long endEpoch = System.currentTimeMillis();
        result.measuredStartEpochMillis = startEpoch;
        result.measuredEndEpochMillis = endEpoch;
        result.measuredDurationNanos = endNanos - startNanos;
        result.comparedReplayOutputEvents = comparedReplayOutputEvents;

        // Safe to build the ObjectNode tree now, outside the timed window (§5).
        for (WAEvent out : retainedReplay0) {
            result.comparedRecords.add(WAEventJsonFactory.writeEvent(out));
        }

        return failure;
    }

    private static boolean shouldRetain(String mode, Set<Integer> sampleIndexSet, long absoluteOutputIndex) {
        return switch (mode) {
            case "full" -> true;
            case "sampled" -> sampleIndexSet != null && sampleIndexSet.contains((int) absoluteOutputIndex);
            default -> false; // "disabled"
        };
    }

    private static String describeError(Exception e) {
        Throwable cause = e.getCause() != null ? e.getCause() : e;
        return cause.getClass().getName() + ": " + cause.getMessage();
    }
}
