package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;
import java.net.URLClassLoader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.jar.JarFile;
import java.util.jar.Manifest;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.security.Password;
import com.webaction.uuid.UUID;

/**
 * The subprocess-driven (or in-process-testable) harness driver (docs/INTEGRATION-TESTS.md). Loads a
 * single OpenProcessor jar, discovers and reflectively constructs its core
 * {@code Processor}, feeds it {@code WAEvent}s, and collects what it emits.
 *
 * <p>
 * <b>Classloading design (docs/INTEGRATION-TESTS.md).</b> The operator jar is loaded into a
 * child {@link URLClassLoader} whose parent is this class's own classloader
 * (standard parent-first delegation — nothing special is configured). The parent
 * supplies the mock {@code com.webaction.*} value types ({@link WAEvent}, {@code
 * UUID}, {@code Event}, {@link Password}) that are compiled directly into this
 * harness jar; the OP jar's own {@code com.example.<Op>.*} classes, plus its
 * *shaded-in* copy of {@code OpenProcessorCommon} ({@code EventProcessor}/{@code
 * BuiltInFuncs}/{@code TypeResolver}/{@code Logger}), load from the child. Because
 * the child asks the parent first, and the parent already defines {@code
 * com.webaction.proc.events.WAEvent}
 * (referenced by the OP's shaded {@code EventProcessor<WAEvent>} bound and by
 * {@code Processor}'s own bytecode), there is exactly one {@code WAEvent} {@code Class}
 * object in the JVM — this harness's own compiled-in mock — so a {@code WAEvent}
 * instance created directly in this class (via {@link WAEventJsonFactory}) is
 * type-compatible with the argument the child-loaded {@code Processor.processEvent}
 * expects, with no reflection needed for {@code WAEvent} itself.
 *
 * <p>
 * The seam interfaces ({@code BuiltInFuncs}, {@code TypeResolver}) and the concrete
 * {@code Logger}, by contrast, come from the OP jar's shaded copy (loaded by the
 * child) — this harness never compiles against {@code OpenProcessorCommon}. It talks
 * to them purely via reflection: {@code Logger} is instantiated through its
 * {@code (String, boolean)} constructor; {@code BuiltInFuncs}/{@code TypeResolver}
 * are supplied as {@link Proxy} instances built over the child-loaded interface
 * {@code Class} — a {@code Proxy} created with {@code Proxy.newProxyInstance(child,
 * new Class&lt;?&gt;[]{iface}, handler)} is an instance of that exact child-loaded
 * interface, so it is directly assignable to the core constructor's parameter
 * (itself resolved from the same child loader) with no cast trickery required.
 */
public final class IntegrationProcessor {

    private IntegrationProcessor() {
    }

    /** The on-disk request contract (docs/INTEGRATION-TESTS.md): {opJar, properties, inputFile, outputFile, types}. */
    public static final class Request {
        public String opJar;
        public Map<String, Object> properties = new HashMap<>();
        public String inputFile;
        public String outputFile;
        public Map<String, Object> types;
        /** Optional runtime namespace to inject as the reserved namespace props key; defaults to "inttest". */
        public String namespace;
        /** Optional runtime source name to inject as the reserved sourceName props key; defaults to "source". */
        public String sourceName;
        /**
         * Optional list of {@code properties} keys whose string value must be wrapped in a
         * mock {@link Password} before construction, so an operator's {@code instanceof
         * Password} check (e.g. a JDBC-pool config reading a {@code Password}-typed
         * connection property) succeeds exactly as it would against the real Striim
         * platform type. Absent/{@code null} entries are left as plain strings.
         */
        public List<String> passwordProperties;
        /**
         * Optional {@code udf:} block (docs/INTEGRATION-TESTS.md). When present, {@code
         * opJar} is driven as a bare UDF pipeline ({@link UdfCore}) instead of as an
         * OpenProcessor {@code Processor} ({@link OperatorCore}).
         */
        public UdfSpec udf;
        /**
         * Optional {@code source:} block (docs/INTEGRATION-TESTS.md). When present, {@code
         * opJar} is driven as a READER ({@link SourceCore}) — ticked rather than fed — and {@code
         * inputFile} must hold no events, because a reader consumes none.
         */
        public SourceSpec source;
        /**
         * Optional {@code target:} block (docs/INTEGRATION-TESTS.md). When present, {@code
         * opJar} is driven as a TARGET ({@link TargetCore}) — a {@code RetriableWriter} whose
         * output is the target database rather than emitted events — and {@code outputFile}
         * receives a {@link TargetReport} instead of an event list.
         */
        public TargetSpec target;
        /**
         * Optional {@code assert.jmx:} block (docs/INTEGRATION-TESTS.md). When present, the
         * op's MBean is built, registered and snapshotted after the input loop, before {@code
         * close()}, into {@link JmxSpec#outputFile}. OP cases only.
         */
        public JmxSpec jmx;
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            System.err.println("usage: IntegrationProcessor <request.json>");
            System.exit(2);
            return;
        }
        try {
            Request request = new ObjectMapper().readValue(new File(args[0]), Request.class);
            run(request);
        } catch (Exception e) {
            System.err.println("IntegrationProcessor failed: " + e);
            e.printStackTrace();
            System.exit(1);
        }
    }

    /**
     * Full end-to-end drive per the request contract: reads {@code inputFile}, drives the
     * operator, writes emitted events to {@code outputFile}. Exceptions are left to
     * propagate with a diagnostic message; {@link #main} is responsible for the
     * non-zero exit.
     */
    public static void run(Request request) throws Exception {
        Objects.requireNonNull(request, "request");
        Objects.requireNonNull(request.opJar, "request.opJar is required");
        Objects.requireNonNull(request.inputFile, "request.inputFile is required");
        Objects.requireNonNull(request.outputFile, "request.outputFile is required");

        List<Event> input = WAEventJsonFactory.readInputEvents(Files.readString(Path.of(request.inputFile)));
        if (request.target != null) {
            // A target emits nothing, so the output file carries the run REPORT -- what the writer
            // acked and the position it reports durable. The third observable output, the target
            // database, is asserted from the Python side against the database itself.
            TargetReport report = driveTarget(new File(request.opJar), request.properties,
                    waEventsOnly(input, "a target: case"),
                    request.types, request.passwordProperties, request.target);
            Files.writeString(Path.of(request.outputFile),
                    new ObjectMapper().writeValueAsString(report));
            return;
        }
        List<WAEvent> output = drive(new File(request.opJar), request.properties, input, request.types,
                request.passwordProperties, request.namespace, request.sourceName, request.udf,
                request.source, request.jmx);
        Files.writeString(Path.of(request.outputFile), WAEventJsonFactory.writeEvents(output));
    }

    /**
     * Drives {@code targetJar} as a Striim Target: feed every input event with its scripted
     * position, restart once if the case asked, flush, and report what the writer acked and holds.
     *
     * <p><b>The flush is explicit and it has to be.</b> {@code AbstractWriterApp} applies on a
     * count or an age deadline, and a case's input is almost always below both — so without a
     * closing flush the events would sit in the accumulator, the target database would be empty,
     * and the case would read as a writer defect. {@code close()} deliberately does not flush
     * either: anything unapplied was never acked, and replaying it is more correct than a
     * last-gasp write during shutdown.</p>
     */
    static TargetReport driveTarget(File targetJar, Map<String, Object> properties,
            List<WAEvent> input, Map<String, Object> types, List<String> passwordProperties,
            TargetSpec target) throws Exception {
        requireCoherentTarget(target, input);

        // A writer resolves a source event's columns through its typeUUID, so a target case needs
        // the same `types:` linkage an OP case does -- and needs it stamped before the first event
        // is handed over, not after.
        SourceTypes sourceTypes = SourceTypes.from(types);
        sourceTypes.stamp(input);

        String distributionId = target.distributionId == null ? "inttest" : target.distributionId;
        TargetCore core = TargetCore.build(targetJar, properties, passwordProperties, distributionId,
                sourceTypes, target.withPositions());
        TargetReport report = new TargetReport();
        try {
            int next = 0;
            final java.util.Set<Integer> fired = new java.util.HashSet<>();
            final java.util.Set<Integer> midRunFired = new java.util.HashSet<>();
            while (next < input.size()) {
                int ordinal = next + 1;
                core.accept(input.get(next), target.withPositions()
                        ? new com.webaction.recovery.ImmutableStemma(
                                POSITION_COMPONENT, distributionId, new OrdinalPosition(ordinal))
                        : null);
                report.eventsAccepted++;
                next++;

                // A SET of already-fired points, not a single boolean: with more than one
                // restart point the driver passes each ordinal repeatedly, because a restart
                // rewinds `next` to the durable position and the same ordinals are fed again.
                // Firing on every pass would restart forever.
                // The mid-run gate comes FIRST, before the restart check, and the order is
                // deliberate: a case that does both at the same ordinal means "change the
                // database, THEN restart into it". The reverse would restart into the old state
                // and apply the change to a writer that had already re-read its metadata, which
                // is a different scenario and not the one anyone writing both would mean.
                if (target.midRunAfter != null && target.midRunAfter.contains(next)
                        && midRunFired.add(next)) {
                    awaitMidRunSql(target, next);
                }

                if (target.restartAfter == null || !target.restartAfter.contains(next)
                        || !fired.add(next)) {
                    continue;
                }
                // BEFORE the restart, so a case can tell "the position survived the restart" from
                // "the position was never there" -- read afterwards the two are the same number.
                // The FIRST restart's value is kept: it is the one a case can compare against
                // "the position was never there", and overwriting it on a later restart would
                // silently change what a two-restart case is asserting.
                if (report.restarts == 0) {
                    report.durablePositionBeforeRestart = core.durablePosition();
                }
                core.restart();
                report.restarts++;
                // THE POINT OF THE TIER. A restart does not skip ahead: the source resumes from
                // what the target says is durable, so everything above that position -- including
                // whatever sat unapplied in the accumulator, since close() deliberately does not
                // flush -- is fed again. A writer that double-writes on replay shows it in the
                // target table, which is the one place a fake driver could never reveal it.
                long durable = core.durableOrdinal();
                if (durable > next) {
                    // A writer cannot be durable past what it was ever handed. Reported rather
                    // than obeyed: obeying it would silently SKIP events, which is precisely the
                    // data loss an over-advanced position causes in production -- the harness
                    // would be reproducing the defect instead of reporting it.
                    throw new IllegalStateException("after restarting, the target reports position "
                            + durable + " as durable, but only " + next + " event(s) had been fed."
                            + " A position ahead of the stream makes the source resume past data"
                            + " it never delivered.");
                }
                int resumeFrom = (int) durable;
                report.eventsReplayed += next - resumeFrom;
                next = resumeFrom;
            }
            core.flush();
            report.durablePosition = core.durablePosition();
            report.ackedEvents = core.ackedEvents();
            report.ackedPositions = core.ackedPositions();
            report.acknowledgeable = core.isAcknowledgeable();
            // AFTER the closing flush, so the counts describe work that is durable and acked --
            // reading them earlier would report a PROCESSED of zero for a run that wrote
            // everything, which is precisely the misreading §43.24 is about.
            report.monitor = core.monitorEvents();
            // What the writer handed to the exception store, with each event named by
            // its ordinal in the input so a case can say which source events a skipped row
            // carried. Identity, not equality: the writer hands over the very objects it was fed.
            for (com.webaction.runtime.NotifyExceptionStore.Notification n
                    : com.webaction.runtime.NotifyExceptionStore.drain()) {
                TargetReport.ExceptionStoreEntry entry = new TargetReport.ExceptionStoreEntry();
                entry.reason = n.reason;
                entry.cause = n.cause;
                for (com.webaction.event.Event e : n.events) {
                    int at = -1;
                    for (int i = 0; i < input.size() && at < 0; i++) {
                        if (input.get(i) == e) {
                            at = i + 1;
                        }
                    }
                    entry.events.add(at);
                }
                report.exceptionStore.add(entry);
            }
        } catch (Exception driveFailure) {
            // A target holds a real connection, so it is closed even on a failed event -- but a
            // throwing close must not REPLACE the failure that says what went wrong, which is what
            // a bare finally does. SourceCore takes the same care for the same reason.
            try {
                core.close();
            } catch (Exception closeFailure) {
                driveFailure.addSuppressed(closeFailure);
            }
            throw driveFailure;
        }
        // Outside the catch, so a close() that fails after a clean drive is reported rather than
        // swallowed -- for this writer close() is where cleanup() releases the JDBC connection,
        // and a leak there is exactly the silent per-module defect AbstractWriterApp.close()
        // exists to prevent.
        core.close();
        return report;
    }

    /**
     * The component a scripted position is attributed to.
     *
     * <p>Fixed rather than generated, because a case asserts the durable position and a per-run
     * UUID would make that assertion unwritable. {@code -1} keeps it clear of {@link LoadedOp}'s
     * {@code types:} UUIDs, which count up from 1 in the same {@code time=0} space.</p>
     */
    private static final UUID POSITION_COMPONENT = new UUID(0L, -1L);

    /** Rejects a {@code target:} block the harness cannot honour, naming what to change. */
    private static void requireCoherentTarget(TargetSpec target, List<WAEvent> input) {
        if (target.restartAfter != null) {
            for (final Integer point : target.restartAfter) {
                if (point == null || point < 1) {
                    throw new IllegalArgumentException("target.restart_after must be at least 1,"
                            + " got " + point + ". A restart before the first event proves nothing:"
                            + " the writer would come back up having written nothing and having"
                            + " nothing to resume from.");
                }
                if (point >= input.size()) {
                    throw new IllegalArgumentException("target.restart_after is " + point
                            + " but the case feeds only " + input.size() + " event(s), so the"
                            + " restart would happen after the last one and no event would replay."
                            + " Lower it below the event count.");
                }
            }
        }
        if (target.midRunAfter != null) {
            for (final Integer point : target.midRunAfter) {
                // Unlike restart_after, the LAST ordinal is allowed: running SQL after the final
                // event and before the closing flush is a real scenario -- it is how a case alters
                // the target between the last accept() and the flush that writes it.
                if (point == null || point < 1 || point > input.size()) {
                    throw new IllegalArgumentException("target.mid_run names ordinal " + point
                            + " but the case feeds " + input.size() + " event(s). It must be"
                            + " between 1 and that count: the driver gates AFTER feeding that many,"
                            + " so an ordinal beyond the stream never fires and the SQL would"
                            + " silently never run.");
                }
            }
        }
    }

    /**
     * The input list as {@code WAEvent}s, for a driver that takes nothing else.
     *
     * <p>A {@code target:} case is fed Striim stream events; only an {@code op:} case's Processor
     * can take another kind. Naming the offending index matters — a 40-event fixture with one
     * stray {@code kind} is otherwise a bisect.</p>
     */
    static List<WAEvent> waEventsOnly(List<Event> input, String driver) {
        List<WAEvent> events = new ArrayList<>(input.size());
        for (int i = 0; i < input.size(); i++) {
            Event event = input.get(i);
            if (!(event instanceof WAEvent waEvent)) {
                throw new IllegalArgumentException(driver + " is fed WAEvents, but fixture event "
                        + i + " is a " + (event == null ? "null" : event.getClass().getSimpleName())
                        + "; check its `kind`.");
            }
            events.add(waEvent);
        }
        return events;
    }

    /**
     * The in-process-testable core: load {@code opJar}, construct its {@code Processor}
     * (injecting {@code props}/{@code Logger}/{@code BuiltInFuncs}/{@code TypeResolver} by
     * constructor parameter type — constructor injection below), invoke its optional {@code start()}
     * lifecycle hook (mirroring {@code AbstractOpenProcessorApp.start()}'s call to
     * {@code processor.start()} right after {@code buildProcessor}; tolerated as absent, as
     * {@code close()} already is), call {@code processEvent} for each input event in order,
     * accumulate emitted events, then {@code close()} the operator.
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types) throws Exception {
        return drive(opJar, properties, input, types, null, null, null);
    }

    /**
     * Overload of {@link #drive(File, Map, List, Map)} that also injects the reserved
     * namespace/sourceName props keys (mirrors {@code AbstractOpenProcessorApp.NAMESPACE_KEY}/
     * {@code SOURCE_NAME_KEY} in OpenProcessorCommon, injected there from the framework in
     * production) so a type-creating core under test can build target type names. Both are
     * optional; {@code null} falls back to the harness defaults ("inttest"/"source").
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types, String namespace, String sourceName) throws Exception {
        return drive(opJar, properties, input, types, null, namespace, sourceName);
    }

    /**
     * Overload of {@link #drive(File, Map, List, Map, String, String)} that also wraps the
     * named {@code properties} entries in a mock {@link Password} before construction (see
     * {@link Request#passwordProperties}), so a core whose config reads a Password-typed
     * connection property (e.g. a JDBC pool's password) sees the same {@code instanceof
     * Password} success it would against the real Striim platform type.
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName) throws Exception {
        return drive(opJar, properties, input, types, passwordProperties, namespace, sourceName, null);
    }

    /**
     * Overload of {@link #drive(File, Map, List, Map, List, String, String)} that also
     * accepts an optional {@code udf:} block (docs/INTEGRATION-TESTS.md). {@code udf ==
     * null} drives {@code opJar} exactly as before, via {@link OperatorCore}; a non-null
     * {@code udf} drives it as a bare UDF pipeline via {@link UdfCore} instead -- neither
     * branch's behavior otherwise changes.
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName, UdfSpec udf) throws Exception {
        return drive(opJar, properties, input, types, passwordProperties, namespace, sourceName, udf, null);
    }

    /**
     * Overload of {@link #drive(File, Map, List, Map, List, String, String, UdfSpec)} that also
     * accepts an optional {@code source:} block (docs/INTEGRATION-TESTS.md). {@code source ==
     * null} drives {@code opJar} exactly as before; a non-null {@code source} drives it as a
     * READER via {@link SourceCore} instead -- ticked {@code source.maxTicks} times rather than
     * fed {@code input}, which for a reader is necessarily empty.
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName, UdfSpec udf, SourceSpec source) throws Exception {
        return drive(opJar, properties, input, types, passwordProperties, namespace, sourceName, udf, source, null);
    }

    /**
     * Overload that also takes an optional {@code assert.jmx:} request. {@code jmx == null} drives
     * exactly as before. Otherwise the op's MBean is snapshotted ({@link JmxSnapshot}) after the
     * last event and before {@code close()}, and the snapshot is written to {@code jmx.outputFile}.
     * A snapshot that could not be taken is written as {@code {"error": ...}}, never omitted.
     */
    public static List<WAEvent> drive(File opJar, Map<String, Object> properties, List<Event> input, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName, UdfSpec udf, SourceSpec source,
            JmxSpec jmx) throws Exception {
        if (jmx != null && (udf != null || source != null)) {
            throw new IllegalArgumentException("assert.jmx is supported for op: cases only, not for a"
                    + (udf != null ? " udf:" : " source:") + " case");
        }
        if (jmx != null && (jmx.outputFile == null || jmx.outputFile.isBlank())) {
            throw new IllegalArgumentException("jmx.outputFile is required");
        }
        if (!opJar.isFile()) {
            throw new IllegalArgumentException("opJar does not exist or is not a file: " + opJar);
        }
        requireNoPropertiesForUdf(udf, properties, passwordProperties);
        requireCoherentSource(source, udf, input);

        if (source != null) {
            return driveSource(opJar, properties, types, passwordProperties, namespace, sourceName, source);
        }

        EventDriver core = udf == null
                ? OperatorCore.build(opJar, properties, types, input, passwordProperties, namespace, sourceName)
                : UdfCore.build(opJar, udf, types);
        List<WAEvent> output = new ArrayList<>();
        for (Event event : input) {
            output.addAll(core.processEvent(event));
        }
        if (jmx != null) {
            // BEFORE close(): a closed core may have released what its attributes read.
            Map<String, Object> snapshot = JmxSnapshot.capture(opJar, ((OperatorCore) core).instance(),
                    jmx.bean, namespace, sourceName);
            Files.writeString(Path.of(jmx.outputFile), new ObjectMapper().writeValueAsString(snapshot));
        }
        core.close();
        return output;
    }

    /**
     * The reader drive loop: tick until the case's expectation is met, or until the budget runs
     * out.
     *
     * <p><b>The budget is a count and the shortfall is loud.</b> Falling short is reported here,
     * naming both numbers, rather than left to the WAEvent comparison — a half-delivered stream
     * otherwise surfaces as "expected 12 events, got 7", which reads like an op defect and is
     * not one.</p>
     */
    private static List<WAEvent> driveSource(File opJar, Map<String, Object> properties, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName, SourceSpec source) throws Exception {
        SourceCore core = SourceCore.build(opJar, properties, types, passwordProperties, namespace, sourceName);
        // A reader consumes nothing, so this carries no data: it is the tick trigger the
        // EventDriver contract needs an argument for, and SourceCore ignores it.
        WAEvent tickTrigger = new WAEvent();
        List<WAEvent> output = new ArrayList<>();
        int ticks = 0;
        try {
            while (ticks < source.maxTicks) {
                output.addAll(core.processEvent(tickTrigger));
                ticks++;
                // The post-start handshake, after the FIRST tick and only once. One tick is what
                // starts the source -- for a change-stream reader it is where the stream query
                // opens and its start timestamp is fixed -- so a commit made now is one the stream
                // can actually see, and a commit made before this point is one it never will.
                if (ticks == 1 && source.isPostStartSeed()) {
                    awaitPostStartSeed(source);
                }
                if (source.expectEvents != null && output.size() >= source.expectEvents) {
                    break;
                }
            }
        } catch (Exception e) {
            // A reader holds real resources (a client, a connection), so it is closed even on a
            // failed tick -- but a throwing close must not REPLACE the tick's exception, which is
            // the one that says what went wrong. A bare finally does exactly that.
            try {
                core.close();
            } catch (Exception closeFailure) {
                e.addSuppressed(closeFailure);
            }
            throw e;
        }
        core.close();
        if (source.expectEvents != null && output.size() < source.expectEvents) {
            throw new IllegalStateException("source: expected at least " + source.expectEvents
                    + " event(s) but the reader emitted " + output.size() + " in " + ticks
                    + " tick(s), its whole max_ticks budget. The budget is a COUNT, not a clock:"
                    + " raise max_ticks if the source genuinely needs more turns to deliver, and"
                    + " do not add a wall-clock wait -- a slow machine must be able to take longer"
                    + " without changing the result.");
        }
        return output;
    }

    /**
     * Defense in depth, mirroring {@code inttest.manifest.load_manifest}'s own guards. A
     * {@code source:} case has no input events (a reader consumes none) and cannot also be a
     * {@code udf:} case (a UDF has no tick), so either combination is an authoring mistake the
     * Python loader already rejects at collection time -- this repeats the check for a
     * direct-Java caller, and validates the counts the wire format cannot.
     */
    static void requireCoherentSource(SourceSpec source, UdfSpec udf, List<? extends Event> input) {
        if (source == null) {
            return;
        }
        if (udf != null) {
            throw new IllegalArgumentException("a case cannot declare both 'source:' and 'udf:'"
                    + " -- a reader is ticked and a UDF is a bare static function, and the harness"
                    + " drives exactly one module per subprocess");
        }
        if (input != null && !input.isEmpty()) {
            throw new IllegalArgumentException("a 'source:' case has no input events (a reader"
                    + " PULLS from its source rather than being fed), but " + input.size()
                    + " were supplied; they would be silently ignored");
        }
        if (source.maxTicks == null || source.maxTicks < 1) {
            throw new IllegalArgumentException(
                    "source.max_ticks is required and must be at least 1, got " + source.maxTicks);
        }
        if (source.expectEvents != null && source.expectEvents < 0) {
            throw new IllegalArgumentException(
                    "source.expect_events cannot be negative, got " + source.expectEvents);
        }
    }

    /**
     * Defense in depth mirroring {@code inttest.manifest.load_manifest}'s own guard:
     * a {@code udf:} case has no constructor to configure,
     * so a non-empty {@code properties}/{@code passwordProperties} alongside a
     * non-null {@code udf} is an authoring mistake the Python loader already rejects
     * at collection time for every YAML-driven test -- this repeats the check here so
     * a direct-Java caller (a JUnit test, or a hand-built request file) gets the same
     * loud failure instead of the value being silently ignored.
     */
    static void requireNoPropertiesForUdf(UdfSpec udf, Map<String, Object> properties, List<String> passwordProperties) {
        if (udf == null) {
            return;
        }
        if (properties != null && !properties.isEmpty()) {
            throw new IllegalArgumentException("properties has no meaning for a udf: case; it was not consulted");
        }
        if (passwordProperties != null && !passwordProperties.isEmpty()) {
            throw new IllegalArgumentException("passwordProperties has no meaning for a udf: case; it was not consulted");
        }
    }

    static Constructor<?> solelyConstructorOf(Class<?> coreClass) {
        Constructor<?>[] ctors = coreClass.getDeclaredConstructors();
        if (ctors.length != 1) {
            throw new IllegalStateException(
                    coreClass.getName() + " must declare exactly one constructor (the Gold Standard rule, docs/INTEGRATION-TESTS.md); found " + ctors.length);
        }
        return ctors[0];
    }

    static String readServiceImplementation(File opJar) throws Exception {
        try (JarFile jar = new JarFile(opJar)) {
            Manifest manifest = jar.getManifest();
            if (manifest == null) {
                throw new IllegalStateException("Op jar has no MANIFEST.MF: " + opJar);
            }
            String impl = manifest.getMainAttributes().getValue("Striim-Service-Implementation");
            if (impl == null || impl.isBlank()) {
                throw new IllegalStateException("Op jar manifest is missing Striim-Service-Implementation: " + opJar);
            }
            return impl.trim();
        }
    }

    /**
     * Every main manifest attribute of {@code opJar}, as a plain map.
     *
     * <p>{@link #readServiceImplementation} reads the one attribute an OpenProcessor needs and
     * fails loudly when it is absent. A Target needs two — the implementation class and
     * {@code Striim-Service-Interface}, which is what lets {@link TargetCore} refuse a jar that is
     * not a writer — and the absent case is the caller's to word, since "no interface declared" is
     * tolerable and "no implementation declared" is not.</p>
     */
    static Map<String, String> manifestAttributes(File opJar) throws Exception {
        Map<String, String> attributes = new LinkedHashMap<>();
        try (JarFile jar = new JarFile(opJar)) {
            Manifest manifest = jar.getManifest();
            if (manifest == null) {
                throw new IllegalStateException("Op jar has no MANIFEST.MF: " + opJar);
            }
            manifest.getMainAttributes().forEach((key, value) ->
                    attributes.put(String.valueOf(key), String.valueOf(value).trim()));
        }
        return attributes;
    }

    /**
     * Parses {@code request.types} (docs/INTEGRATION-TESTS.md) into an ordered
     * {@code sourceTableName -> orderedColumnNames} map. Absent ({@code null}) `types`
     * yields an empty map; entries whose value is not a {@code List} are ignored.
     */
    static Map<String, List<String>> parseTypeSchemas(Map<String, Object> types) {
        Map<String, List<String>> schemasByTable = new LinkedHashMap<>();
        if (types == null) {
            return schemasByTable;
        }
        for (Map.Entry<String, Object> entry : types.entrySet()) {
            List<?> rawColumns = columnListOf(entry.getValue());
            if (rawColumns != null) {
                List<String> columns = new ArrayList<>(rawColumns.size());
                for (Object column : rawColumns) {
                    columns.add(String.valueOf(column));
                }
                schemasByTable.put(entry.getKey(), columns);
            }
        }
        return schemasByTable;
    }

    /** The column list of either `types:` form: a bare list, or a mapping's `columns`. */
    static List<?> columnListOf(Object spec) {
        if (spec instanceof List<?> list) {
            return list;
        }
        if (spec instanceof Map<?, ?> map && map.get("columns") instanceof List<?> list) {
            return list;
        }
        return null;
    }

    /** Key column names declared for a table; empty for the bare list form. */
    static List<String> keysOf(Object spec) {
        List<String> keys = new ArrayList<>();
        if (spec instanceof Map<?, ?> map && map.get("keys") instanceof List<?> raw) {
            for (Object k : raw) {
                keys.add(String.valueOf(k));
            }
        }
        return keys;
    }

    /** Column -> display alias declared for a table; empty for the bare list form. */
    static Map<String, String> aliasesOf(Object spec) {
        Map<String, String> aliases = new LinkedHashMap<>();
        if (spec instanceof Map<?, ?> map && map.get("aliases") instanceof Map<?, ?> raw) {
            raw.forEach((k, v) -> aliases.put(String.valueOf(k), String.valueOf(v)));
        }
        return aliases;
    }

    /** Injects each Gold Standard constructor parameter by type (constructor injection below). */
    static Object resolveConstructorArgument(String coreClassName, Class<?> paramType, Map<String, Object> properties, Map<UUID, List<String>> columnsByUuid,
            Map<UUID, MetaInfo.Type> declaredByUuid, Map<String, MetaInfo.Type> declaredByName, URLClassLoader child,
            List<String> passwordProperties, String namespace, String sourceName) throws Exception {
        if (Map.class.isAssignableFrom(paramType)) {
            return enrichedProperties(properties, passwordProperties, namespace, sourceName);
        }
        String simpleName = paramType.getSimpleName();
        return switch (simpleName) {
            case "Logger" -> newLogger(paramType, properties);
            case "BuiltInFuncs" -> newBuiltInFuncsProxy(paramType, columnsByUuid, child);
            case "TypeResolver" -> newTypeResolverProxy(paramType, declaredByUuid, declaredByName, child);
            // Anything else is an op-specific seam -- the readers' seams have no overlap with
            // each other, so no shared type can absorb them. Ask the op.
            // The seam gets the SAME enriched, defensive copy the core's Map parameter gets:
            // `properties` is legitimately nullable, carries no reserved keys, and leaves
            // Password-typed values as bare strings -- a seam built from a different view of the
            // case than the core it is built for is a bug generator. A copy, so a seam that
            // mutates cannot change what the core then receives.
            default -> IntegrationSeamsLookup.seamFor(
                    corePackageOf(coreClassName), paramType,
                    enrichedProperties(properties, passwordProperties, namespace, sourceName),
                    child, coreClassName);
        };
    }

    /**
     * The properties view a driven core sees: a defensive copy, the reserved namespace/sourceName
     * keys added, and the named Password-typed entries wrapped. Extracted so the {@code Map}
     * parameter and an op's {@code IntegrationSeams} are handed the SAME view of the case.
     */
    static Map<String, Object> enrichedProperties(Map<String, Object> properties,
            List<String> passwordProperties, String namespace, String sourceName) {
        Map<String, Object> props = properties == null ? new HashMap<>() : new HashMap<>(properties);
        // Reserved keys cross-referencing AbstractOpenProcessorApp.NAMESPACE_KEY/SOURCE_NAME_KEY
        // (OpenProcessorCommon) — literal here since this harness never compiles against that jar.
        props.put("striim.op.namespace", namespace != null ? namespace : "inttest");
        props.put("striim.op.sourceName", sourceName != null ? sourceName : "source");
        // Password-typed connection properties (Request.passwordProperties) arrive as plain
        // strings (everything in `properties` is JSON-deserialized text); wrap the named entries
        // so a core's `instanceof Password` check succeeds as it would against the platform type.
        // A present-but-empty value is left as a plain (empty) string, not wrapped: prefix
        // inheritance (a "Bootstrap" override falling back to the main connection) treats an
        // empty STRING as unset, and wrapping "" would defeat that fallback and silently
        // authenticate with no password instead of inheriting the main one.
        if (passwordProperties != null) {
            for (String key : passwordProperties) {
                Object value = props.get(key);
                if (value != null && !value.toString().isEmpty()) {
                    props.put(key, new Password(value.toString()));
                }
            }
        }
        return props;
    }

    /** The op's core package. Guarded here rather than relying on a caller's earlier validation. */
    static String corePackageOf(String coreClassName) {
        int lastDot = coreClassName.lastIndexOf('.');
        if (lastDot < 0) {
            throw new IllegalStateException(
                    "core class name has no package, so its IntegrationSeams cannot be located: "
                            + coreClassName);
        }
        return coreClassName.substring(0, lastDot);
    }

    /**
     * A source {@code MetaInfo.Type} carrying the declared columns in declaration order.
     * {@code MetaInfo.Type}'s state is public fields, and this fleet's own tests construct it
     * the same way (it is on the platform floor precisely because it is freely constructible).
     * A {@code LinkedHashMap} because column ORDER is the whole point — an index-mapping core
     * reads position, not just membership.
     */
    static MetaInfo.Type newSourceType(UUID uuid, List<String> columns, String tableName) {
        return newSourceType(uuid, columns, tableName, List.of(), Map.of());
    }

    static MetaInfo.Type newSourceType(UUID uuid, List<String> columns, String tableName,
            List<String> keyFields, Map<String, String> aliases) {
        MetaInfo.Type type = new MetaInfo.Type(uuid);
        // A `types:` key is the metadata TableName, conventionally "<namespace>.<name>" --
        // the same two-part shape a real MDR type carries, and the same shape a core splits
        // back apart to build a derived type name. Split on the FIRST dot; with no dot the
        // whole key is the name and the namespace falls back to the harness's own.
        if (tableName != null) {
            int dot = tableName.indexOf('.');
            type.nsName = dot > 0 ? tableName.substring(0, dot) : "inttest";
            type.name = dot > 0 ? tableName.substring(dot + 1) : tableName;
        }
        Map<String, String> fields = new LinkedHashMap<>();
        for (String column : columns) {
            fields.put(column, "java.lang.String");
        }
        type.fields = fields;
        type.keyFields = new ArrayList<>(keyFields);
        type.fieldAlias = new LinkedHashMap<>(aliases);
        return type;
    }

    /**
     * The core's {@code Logger}, built from the case's {@code EnableLogging} and {@code LogSink}
     * properties the way an App builds it ({@code new Logger(tag, enableLogging,
     * LogSinkSelection.resolve(props.get("LogSink"), null))}), so a case can turn debug on and
     * pick a sink. Keys match case-insensitively, as on a node. By reflection: this harness never
     * compiles against OpenProcessorCommon. A Common without {@code LogSinkSelection} (an older
     * Common) gets the two-arg constructor. The module's own default is not known here and is
     * passed as null; node-level logger configuration still applies inside
     * {@code resolve}.
     */
    static Object newLogger(Class<?> loggerClass, Map<String, Object> properties) throws Exception {
        boolean enableLogging = Boolean.parseBoolean(String.valueOf(propertyIgnoringCase(properties, "EnableLogging")));
        Class<?> selectionClass;
        try {
            selectionClass = Class.forName(loggerClass.getPackageName() + ".LogSinkSelection", true,
                    loggerClass.getClassLoader());
        } catch (ClassNotFoundException e) {
            return loggerClass.getConstructor(String.class, boolean.class).newInstance("inttest", enableLogging);
        }
        Object selection = selectionClass.getMethod("resolve", Object.class, String.class)
                .invoke(null, propertyIgnoringCase(properties, "LogSink"), null);
        return loggerClass.getConstructor(String.class, boolean.class, selectionClass)
                .newInstance("inttest", enableLogging, selection);
    }

    /** A property by name, ignoring case as a node's properties map does; null when absent. */
    static Object propertyIgnoringCase(Map<String, Object> properties, String name) {
        if (properties == null) {
            return null;
        }
        for (Map.Entry<String, Object> e : properties.entrySet()) {
            if (name.equalsIgnoreCase(e.getKey())) {
                return e.getValue();
            }
        }
        return null;
    }

    /**
     * A {@code BuiltInFuncs} mock. {@code IS_PRESENT} reads the passed mock {@link WAEvent}'s
     * own presence bitmap directly (comparing the {@code image} argument against the event's
     * {@code data}/{@code before} array by reference to know which bitmap to consult) — no
     * {@code types:} schema needed for that call, matching docs/INTEGRATION-TESTS.md
     *
     * <p>
     * {@code getFieldsArray}/{@code getAliasFieldName} are seeded from {@code columnsByUuid}
     * (built from {@code request.types} and keyed by the per-table UUID {@link #drive} stamped
     * onto each matching input event, above): {@code getFieldsArray} returns one
     * {@link MockSourceFields} placeholder field per declared column (empty array for an
     * unknown/absent UUID); {@code getAliasFieldName} maps each placeholder's name
     * ({@code "f"+i}) to the real column name at that position (empty map for an
     * unknown/absent UUID) — exactly the shape {@code WAEventFactory.getOrBuildSourceIndex}
     * needs to build its source-name→index lookup.
     *
     * <p>
     * <b>Note:</b> "needs {@code types:}" and "takes a {@code TypeResolver} constructor
     * parameter" are independent. A core can resolve column names purely through this
     * {@code BuiltInFuncs} seam — as a lookup OP's field-map builder can, with
     * no {@code TypeResolver} parameter at all — and still need a populated
     * {@code columnsByUuid}/alias map, seeded here only from {@code request.types}. Such
     * a core's {@code test.yaml} still declares {@code types:} even though its
     * constructor never takes a {@code TypeResolver}.
     */
    static Object newBuiltInFuncsProxy(Class<?> iface, Map<UUID, List<String>> columnsByUuid, URLClassLoader child) {
        InvocationHandler handler = (proxy, method, methodArgs) -> {
            switch (method.getName()) {
                case "IS_PRESENT": {
                    WAEvent event = (WAEvent) methodArgs[0];
                    Object[] image = (Object[]) methodArgs[1];
                    int index = (Integer) methodArgs[2];
                    if (image == event.before) {
                        return event.isBeforePresent(index);
                    }
                    return event.isDataPresent(index);
                }
                case "getFieldsArray": {
                    List<String> columns = columnsByUuid.get((UUID) methodArgs[0]);
                    if (columns != null) {
                        return MockSourceFields.fieldsFor(columns);
                    }
                    // A type the mock TypeResolver created: its fields by name, as a generated
                    // platform class reflects them, so a core can prove the created type's order.
                    Field[] named = MockSourceFields.namedFields(MockSourceFields.createdFields(methodArgs[0]));
                    return named != null ? named : MockSourceFields.fieldsFor(null);
                }
                case "getAliasFieldName":
                    return MockSourceFields.aliasesFor(columnsByUuid.get((UUID) methodArgs[0]));
                case "equals":
                    return proxy == methodArgs[0];
                case "hashCode":
                    return System.identityHashCode(proxy);
                case "toString":
                    return "MockBuiltInFuncs";
                default:
                    throw new UnsupportedOperationException("MockBuiltInFuncs." + method.getName());
            }
        };
        return Proxy.newProxyInstance(child, new Class<?>[] { iface }, handler);
    }

    /**
     * A {@code TypeResolver} mock. {@code createType} returns a deterministic, non-null
     * {@code MetaInfo.Type} built from a UUID derived from the requested type name's hash —
     * the harness never emits {@code typeUUID} on output, so the exact value is immaterial;
     * it only has to be non-null so the operator does not NPE building its target event
     * (docs/INTEGRATION-TESTS.md).
     *
     * <p>
     * {@code getTypeByUUID}/{@code getTypeByName} resolve the SOURCE tables declared in
     * {@code types:}, using the very UUIDs {@link OperatorCore#build} minted for them and
     * stamped onto the matching input events — the same linkage the {@code BuiltInFuncs}
     * proxy already uses, so one {@code types:} block feeds both seams and there is no
     * second place to declare a schema. A type-consuming core (e.g. an operator that re-types events reads
     * {@code fields}/{@code keyFields}/{@code fieldAlias} off the returned object
     * reflectively) can therefore run under this harness at all, which it could not while
     * these returned {@code null}.
     *
     * <p>
     * Anything NOT declared in {@code types:} still resolves to {@code null} — a resolver
     * that does not have the type returns null, it does not throw. That is what keeps this
     * additive: a case with no {@code types:} block behaves exactly as it did, and a core
     * asking for a type it is about to CREATE still gets null and takes its create path.
     *
     * <p>
     * {@code fields} values are declared as {@code java.lang.String} for every column: the
     * harness's {@code types:} block carries column NAMES and order, which is what index
     * mapping and column filtering need, and the mock {@code createType} ignores the type
     * strings entirely. {@code keyFields}/{@code fieldAlias} are empty for the same reason —
     * nothing models them yet. If a case ever needs real key/alias metadata, enrich
     * {@code types:} rather than inventing a parallel block.
     */
    static Object newTypeResolverProxy(Class<?> iface, Map<UUID, MetaInfo.Type> declaredByUuid,
            Map<String, MetaInfo.Type> declaredByName, URLClassLoader child) {
        // Types this resolver CREATED, resolvable afterwards by UUID and by name -- because a
        // real MDR registers them and a core relies on that. An operator that re-types events moves an event to a
        // filtered type, then looks the event's (now created) type up again to enrich it further;
        // and it re-verifies a cached type with getTypeByUUID before reusing it. Both miss
        // against a resolver that forgets what it minted, so the second transform silently does
        // nothing -- which is a harness artifact, not operator behaviour, and exactly the kind of
        // false negative an integration tier must not manufacture.
        Map<UUID, MetaInfo.Type> createdByUuid = new LinkedHashMap<>();
        Map<String, MetaInfo.Type> createdByName = new LinkedHashMap<>();

        InvocationHandler handler = (proxy, method, methodArgs) -> {
            switch (method.getName()) {
                case "getTypeByUUID": {
                    UUID uuid = (UUID) methodArgs[0];
                    if (uuid == null) {
                        return null;
                    }
                    MetaInfo.Type created = createdByUuid.get(uuid);
                    return created != null ? created : declaredByUuid.get(uuid);
                }
                case "getTypeByName": {
                    // (namespace, name). Match on the bare table name declared in `types:`,
                    // and on "namespace.name" for a core that qualifies it.
                    Object namespace = methodArgs[0];
                    Object name = methodArgs[1];
                    if (name == null) {
                        return null;
                    }
                    // Namespace-qualified FIRST. Most `types:` keys in the corpus are bare
                    // (ORDERS, CUSTOMERS...), so trying the bare name first would resolve
                    // getTypeByName("anyNamespace", "ORDERS") across namespaces -- handing a core
                    // a bogus non-null for a type it was about to create, and skipping its create
                    // path. The bare form stays as a fallback because a bare key is how a case
                    // usually declares its one table.
                    String qualified = namespace == null ? null : namespace + "." + name;
                    MetaInfo.Type declared = qualified == null ? null : declaredByName.get(qualified);
                    if (declared == null) {
                        declared = declaredByName.get(name.toString());
                    }
                    if (declared != null) {
                        return declared;
                    }
                    // A previously CREATED type, looked up the way a core checks "does my derived
                    // type already exist?". Declared source tables win: a name can only be one.
                    return createdByName.get(qualified == null ? name.toString() : qualified);
                }
                case "createType": {
                    String typeName = (String) methodArgs[0];
                    @SuppressWarnings("unchecked")
                    Map<String, String> fields = (Map<String, String>) methodArgs[1];
                    @SuppressWarnings("unchecked")
                    Map<String, String> aliases = (Map<String, String>) methodArgs[2];
                    @SuppressWarnings("unchecked")
                    Map<String, Boolean> keys = (Map<String, Boolean>) methodArgs[3];

                    // time=1, NOT 0: OperatorCore mints source-table UUIDs as (0, 1..N), and a
                    // created type whose name happened to hash into that range would otherwise
                    // resolve to a source schema. Harmless while getTypeByUUID always returned
                    // null; reachable the moment it stopped.
                    UUID uuid = new UUID(1L, ((long) typeName.hashCode()) & 0xffffffffL);
                    MetaInfo.Type type = new MetaInfo.Type(uuid);
                    // Carry what the caller ASKED to create, not just a UUID: a core that reads
                    // the created type back (its fields, its identity) must see what it built.
                    int dot = typeName.indexOf('.');
                    type.nsName = dot > 0 ? typeName.substring(0, dot) : "inttest";
                    type.name = dot > 0 ? typeName.substring(dot + 1) : typeName;
                    type.fields = fields == null ? new LinkedHashMap<>() : new LinkedHashMap<>(fields);
                    type.fieldAlias = aliases == null ? new LinkedHashMap<>() : new LinkedHashMap<>(aliases);
                    List<String> keyFields = new ArrayList<>();
                    if (keys != null) {
                        keys.forEach((k, v) -> {
                            if (Boolean.TRUE.equals(v)) {
                                keyFields.add(k);
                            }
                        });
                    }
                    type.keyFields = keyFields;

                    createdByUuid.put(uuid, type);
                    createdByName.put(typeName, type);
                    MockSourceFields.registerCreated(uuid, new ArrayList<>(type.fields.keySet()));
                    return type;
                }
                case "equals":
                    return proxy == methodArgs[0];
                case "hashCode":
                    return System.identityHashCode(proxy);
                case "toString":
                    return "MockTypeResolver";
                default:
                    throw new UnsupportedOperationException("MockTypeResolver." + method.getName());
            }
        };
        return Proxy.newProxyInstance(child, new Class<?>[] { iface }, handler);
    }

    /**
     * Signals that the source has started, then blocks until the caller's seed SQL has committed.
     *
     * <p><b>Why the tick loop blocks rather than the caller racing it.</b> The alternative — the
     * Python side seeding as soon as it has spawned this process — is a race it cannot win: it has
     * no way to know whether the stream query is open yet, and a commit that lands first is
     * invisible to the stream. Blocking here makes the ordering a fact rather than a hope.</p>
     *
     * <p><b>The timeout fails the run rather than continuing.</b> Continuing would produce a
     * reader that emits nothing and an error message about tick budgets — the operator blamed for
     * a harness problem, which is the failure mode this whole handshake exists to remove.</p>
     */
    /**
     * Pauses after event {@code ordinal} while the caller runs SQL against the live target.
     *
     * <p>Same rendezvous as {@link #awaitPostStartSeed} and for the same reason — two one-way
     * files, because a file either exists or does not and cannot half-arrive — but keyed by
     * ORDINAL, so one run can gate several times.</p>
     *
     * <p><b>The wait is INSIDE the feed loop, between two events.</b> That is the whole point: the
     * writer is up, it has resolved its metadata, and it has applied whatever the earlier events
     * produced. Anything the caller does here lands on a live writer rather than on a clean one,
     * which is the only way to reach {@code verifyUnchanged} or a restart whose checkpoint is
     * behind.</p>
     *
     * <p><b>Timing out fails the run.</b> Continuing would feed the rest of the stream against a
     * database the case believes it changed, and the assertion that followed would be about a
     * state nobody arranged — a harness problem reported as a writer defect.</p>
     */
    private static void awaitMidRunSql(TargetSpec target, int ordinal) throws Exception {
        if (target.midRunGateDir == null) {
            throw new IllegalStateException("target.mid_run names ordinal " + ordinal
                    + " but no gate directory was supplied, so there is nobody to run the SQL."
                    + " The caller must pass midRunGateDir whenever midRunAfter is set.");
        }
        java.io.File dir = new java.io.File(target.midRunGateDir);
        java.io.File ready = new java.io.File(dir, "midrun." + ordinal + ".ready");
        java.io.File go = new java.io.File(dir, "midrun." + ordinal + ".go");

        if (!ready.createNewFile() && !ready.exists()) {
            throw new IllegalStateException("target: could not signal mid-run readiness at "
                    + ready.getAbsolutePath());
        }

        long budget = (target.midRunTimeoutMillis != null && target.midRunTimeoutMillis > 0)
                ? target.midRunTimeoutMillis.longValue() : POST_START_SEED_TIMEOUT_MILLIS;
        long deadline = System.currentTimeMillis() + budget;
        while (!go.exists()) {
            if (System.currentTimeMillis() > deadline) {
                throw new IllegalStateException("target: mid_run after event " + ordinal
                        + " -- waited " + (budget / 1000) + "s for the caller to run its SQL and"
                        + " signal " + go.getAbsolutePath() + ", and it never appeared. The writer"
                        + " is fine; the handshake is not.");
            }
            Thread.sleep(POST_START_SEED_POLL_MILLIS);
        }
    }

    private static void awaitPostStartSeed(SourceSpec source) throws Exception {
        java.io.File dir = new java.io.File(source.seedGateDir);
        java.io.File ready = new java.io.File(dir, "seed.ready");
        java.io.File go = new java.io.File(dir, "seed.go");

        if (!ready.createNewFile() && !ready.exists()) {
            throw new IllegalStateException("source: could not signal post-start readiness at "
                    + ready.getAbsolutePath());
        }

        long budget = (source.seedGateTimeoutMillis != null && source.seedGateTimeoutMillis > 0)
                ? source.seedGateTimeoutMillis.longValue() : POST_START_SEED_TIMEOUT_MILLIS;
        long deadline = System.currentTimeMillis() + budget;
        while (!go.exists()) {
            if (System.currentTimeMillis() > deadline) {
                throw new IllegalStateException("source: seed_when: post_start -- waited "
                        + (budget / 1000) + "s after the first tick for the"
                        + " caller to commit its seed and signal " + go.getAbsolutePath()
                        + ", and it never appeared. The reader is fine; the handshake is not.");
            }
            Thread.sleep(POST_START_SEED_POLL_MILLIS);
        }
    }

    /**
     * How long the reader waits for its seed.
     *
     * <p><b>A FALLBACK only — the caller normally supplies the budget</b>
     * ({@code SourceSpec.seedGateTimeoutMillis}, half of its own timeout). Two revisions got this
     * wrong in opposite directions: first the same 120s as the caller's default, so the caller
     * always expired first and the diagnostic below could never print; then a hardcoded 60s, which
     * a review pointed out breaks again on any case that sets {@code timeout:} below 120. A value
     * derived from the caller's actual budget is the only one that stays correct.</p>
     */
    private static final long POST_START_SEED_TIMEOUT_MILLIS = 60_000L;

    /** Poll interval for the go file. A wall-clock wait INSIDE the run, deliberately: this is the
     * one place the tick model cannot express, because the thing being waited for is another
     * process committing a transaction. It bounds a handshake, never an assertion. */
    private static final long POST_START_SEED_POLL_MILLIS = 50L;
}
