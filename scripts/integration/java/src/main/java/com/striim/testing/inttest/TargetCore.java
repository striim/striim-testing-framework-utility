package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.Path;
import com.webaction.recovery.Position;
import com.webaction.security.Password;

/**
 * Drives a TARGET — a {@code RetriableWriter}, which emits nothing and writes to a database.
 *
 * <h2>Why it does not reuse {@link LoadedOp}</h2>
 * {@code LoadedOp} derives the core class by appending {@code .Processor} to the package named in
 * {@code Striim-Service-Implementation}, because an OpenProcessor's App is a wrapper around a
 * separate {@code Processor}. <b>A Target has no such split</b>: the class the manifest names IS
 * the writer, it is constructed by the platform through a public no-arg constructor, and it is
 * configured through {@code init(Map, Map, UUID, String)} rather than by constructor injection.
 * Nothing of {@code LoadedOp}'s sequence survives that, so this class loads its own.
 *
 * <h2>The {@code Acknowledgeable} gate is reproduced, not shortcut</h2>
 * The platform injects {@code receiptCallback} only when the adapter implements
 * {@code Acknowledgeable}; a writer without the marker is never called back and silently loses
 * whatever it had not committed. Injecting unconditionally here would make this tier pass for a
 * writer that fails in production, so the gate is checked exactly as {@code Target} checks it.
 */
final class TargetCore implements TargetDriver {

    private static final String IMPLEMENTATION = "Striim-Service-Implementation";
    private static final String INTERFACE = "Striim-Service-Interface";
    private static final String WRITER_INTERFACE = "com.webaction.proc.RetriableWriter";

    private final Class<?> writerClass;
    private final Map<String, Object> properties;
    private final String distributionId;
    private final RecordingReceiptCallback receipts;
    private final boolean recovery;

    private Object instance;
    private boolean acknowledgeable;
    private Method publishMonitorEvents;
    private Method processEvent;
    private Method flush;
    private Method endpointCheckpoint;
    private Method close;

    private TargetCore(Class<?> writerClass, Map<String, Object> properties, String distributionId,
            RecordingReceiptCallback receipts, boolean recovery) {
        this.writerClass = writerClass;
        this.properties = properties;
        this.distributionId = distributionId;
        this.receipts = receipts;
        this.recovery = recovery;
    }

    /**
     * Loads {@code targetJar}, constructs the writer it names, and initialises it.
     *
     * @param targetJar          the shaded target jar
     * @param properties         the case's {@code properties:}, already token-rendered
     * @param passwordProperties which of those keys to wrap in a mock {@link Password}
     * @param distributionId     the distribution key the platform would pass to {@code init}
     * @param sourceTypes        the case's {@code types:} linkage, published to the static
     *                           {@code BuiltInFunc} the writer's own resolver reads
     * @param recovery           the application's RECOVERY setting, as the platform passes it
     */
    static TargetCore build(File targetJar, Map<String, Object> properties,
            List<String> passwordProperties, String distributionId, SourceTypes sourceTypes,
            boolean recovery) throws Exception {
        registerSourceTypes(sourceTypes);
        URL jarUrl = targetJar.toURI().toURL();
        URLClassLoader child = new URLClassLoader(new URL[] { jarUrl },
                IntegrationProcessor.class.getClassLoader());

        Map<String, String> manifest = IntegrationProcessor.manifestAttributes(targetJar);
        String declared = manifest.get(INTERFACE);
        if (declared != null && !WRITER_INTERFACE.equals(declared)) {
            throw new IllegalStateException(targetJar + " declares " + INTERFACE + "=" + declared
                    + ", so it is not a Target. A 'target:' case drives a " + WRITER_INTERFACE
                    + "; drop the 'target:' block to drive it as an OpenProcessor instead.");
        }
        String writerClassName = manifest.get(IMPLEMENTATION);
        if (writerClassName == null) {
            throw new IllegalStateException(targetJar + " has no " + IMPLEMENTATION
                    + " manifest attribute, so the harness cannot tell which class to construct");
        }

        Class<?> writerClass;
        try {
            writerClass = Class.forName(writerClassName, true, child);
        } catch (ClassNotFoundException e) {
            throw new IllegalStateException("Target class not found: " + writerClassName
                    + " (from manifest " + IMPLEMENTATION + " in " + targetJar + ")", e);
        }

        TargetCore core = new TargetCore(writerClass, targetProperties(properties, passwordProperties),
                distributionId, new RecordingReceiptCallback(), recovery);
        core.start();
        return core;
    }

    /**
     * Publishes the case's {@code types:} to the static {@code BuiltInFunc} a writer's production
     * {@code BuiltInFuncResolver} reads.
     *
     * <p>Called once per {@link #build}, not per {@link #restart()} — a restart rebuilds the
     * writer, not the case. The registry is JVM-wide and REPLACED rather than merged, which is
     * what keeps a second case in the same JVM from inheriting the first one's schemas.</p>
     */
    private static void registerSourceTypes(SourceTypes sourceTypes) {
        Map<com.webaction.uuid.UUID, java.lang.reflect.Field[]> fields = new LinkedHashMap<>();
        Map<com.webaction.uuid.UUID, Map<String, String>> aliases = new LinkedHashMap<>();
        if (sourceTypes != null) {
            sourceTypes.columnsByUuid.forEach((uuid, columns) -> {
                fields.put(uuid, MockSourceFields.fieldsFor(columns));
                aliases.put(uuid, MockSourceFields.aliasesFor(columns));
            });
        }
        com.webaction.runtime.BuiltInFunc.setSourceTypes(fields, aliases);
    }

    /**
     * The properties view a target sees: a defensive copy with the named Password entries wrapped.
     *
     * <p>Deliberately NOT {@code IntegrationProcessor.enrichedProperties}. That adds the reserved
     * {@code striim.op.namespace}/{@code striim.op.sourceName} keys, which are
     * {@code AbstractOpenProcessorApp} concepts the platform injects for an OpenProcessor. A Target
     * is never given them, and handing them over here would let a writer read a key production
     * will not supply.</p>
     */
    private static Map<String, Object> targetProperties(Map<String, Object> properties,
            List<String> passwordProperties) {
        Map<String, Object> props = properties == null ? new HashMap<>() : new HashMap<>(properties);
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

    /** Constructs the writer, injects the callback if it is entitled to one, and initialises it. */
    private void start() throws Exception {
        try {
            instance = writerClass.getDeclaredConstructor().newInstance();
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(writerClass.getName() + " has no public no-argument"
                    + " constructor. The platform instantiates a Target that way, so a writer"
                    + " without one cannot be deployed at all.", e);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException("Constructing " + writerClass.getName() + " threw",
                    e.getCause() != null ? e.getCause() : e);
        }

        acknowledgeable = instance instanceof com.webaction.recovery.Acknowledgeable;
        if (acknowledgeable) {
            ((com.webaction.proc.RetriableWriter) instance).setReceiptCallback(receipts);
            // A non-null handle is what makes the writer's exception-store route send.
            ((com.webaction.proc.RetriableWriter) instance)
                    .setFlowComponent(new HarnessFlowComponent());
        }

        if (instance instanceof com.webaction.proc.RetriableWriter writer) {
            writer.setRecoveryEnabled(recovery);
        }

        processEvent = method("processEvent", int.class, com.webaction.event.Event.class,
                ImmutableStemma.class);
        flush = method("flush");
        endpointCheckpoint = method("getEndpointCheckpoint");
        publishMonitorEvents = method("publishMonitorEvents",
                com.webaction.runtime.monitor.MonitorEventsCollection.class);
        close = method("close");

        Method init = method("init", Map.class, Map.class, com.webaction.uuid.UUID.class,
                String.class);
        try {
            init.invoke(instance, properties, new LinkedHashMap<String, Object>(), null,
                    distributionId);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException(writerClass.getName() + ".init(...) threw",
                    e.getCause() != null ? e.getCause() : e);
        }
    }

    @Override
    public void accept(WAEvent event, ImmutableStemma position) throws Exception {
        invoke(processEvent, "processEvent", 0, event, position);
    }

    @Override
    public void flush() throws Exception {
        invoke(flush, "flush");
    }

    @Override
    public String durablePosition() throws Exception {
        Object position = invoke(endpointCheckpoint, "getEndpointCheckpoint");
        return render((Position) position);
    }

    /**
     * Renders a position the way {@code AbstractWriterApp} renders it for monitoring: each path's
     * low source position, joined by {@code |}.
     *
     * <p>Sorted, which the writer's own rendering is not. A case asserts this string, and map
     * iteration order is not something a writer should be able to fail on.</p>
     */
    private static String render(Position position) {
        if (position == null || position.isEmpty()) {
            return null;
        }
        List<String> parts = new java.util.ArrayList<>();
        for (Path path : position.values()) {
            parts.add(String.valueOf(path.getLowSourcePosition()));
        }
        java.util.Collections.sort(parts);
        return String.join("|", parts);
    }

    /**
     * The highest ordinal the target reports as durable, or 0 when it reports none.
     *
     * <p>This is what a source resumes from. Everything above it was never acknowledged, so the
     * platform replays it — and a writer that has already applied some of that work must absorb
     * the replay without duplicating it. Reading the raw position rather than parsing
     * {@link #durablePosition()}'s rendered string keeps the resume point independent of how the
     * position happens to print.</p>
     */
    long durableOrdinal() throws Exception {
        Position position = (Position) invoke(endpointCheckpoint, "getEndpointCheckpoint");
        if (position == null) {
            return 0L;
        }
        long highest = 0L;
        for (Path path : position.values()) {
            if (path.getLowSourcePosition() instanceof OrdinalPosition) {
                highest = Math.max(highest,
                        ((OrdinalPosition) path.getLowSourcePosition()).ordinal());
            }
        }
        return highest;
    }

    /**
     * What the writer publishes to the platform's monitor, as name -> value.
     *
     * <p><b>The gap this closes.</b> A writer can write correctly while never overriding
     * {@code publishMonitorEvents}, leaving the platform with no throughput figures. The figures a person reads off a monitor page
     * to decide whether a flow is progressing were, until this, asserted by no one.</p>
     *
     * <p>Values are rendered as strings: a case compares text, and the counts are exact integers
     * whose string form is unambiguous. The clock-valued metrics are published here too, so a
     * report carries them for a human to read -- but a CASE must not assert them, and the manifest
     * loader refuses one that tries.</p>
     */
    Map<String, String> monitorEvents() throws Exception {
        final com.webaction.runtime.monitor.MonitorEventsCollection collection =
                new com.webaction.runtime.monitor.MonitorEventsCollection(System.currentTimeMillis());
        invoke(publishMonitorEvents, "publishMonitorEvents", collection);
        final Map<String, String> out = new LinkedHashMap<>();
        for (final com.webaction.runtime.monitor.MonitorEvent event : collection.getEvents()) {
            out.put(String.valueOf(event.getType()), String.valueOf(event.getValue()));
        }
        return out;
    }

    @Override
    public void restart() throws Exception {
        close();
        start();
    }

    @Override
    public int ackedEvents() {
        return receipts.events();
    }

    /** Every position the target released, in order. */
    List<String> ackedPositions() {
        return receipts.positions();
    }

    /**
     * Closes the writer. Idempotent, and idempotent even when the close itself throws — the
     * instance is released first, so a failed close is reported once rather than re-attempted by
     * whatever cleanup path calls this next.
     */
    @Override
    public void close() throws Exception {
        Object closing = instance;
        if (closing == null) {
            return;
        }
        instance = null;
        try {
            close.invoke(closing);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw new IllegalStateException(writerClass.getName() + ".close() threw", cause);
        }
    }

    /**
     * Resolves one method of the {@code RetriableWriter} contract, naming the contract rather than
     * the reflection call when it is missing.
     */
    private Method method(String name, Class<?>... parameterTypes) {
        try {
            return writerClass.getMethod(name, parameterTypes);
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(writerClass.getName() + " has no " + name
                    + java.util.Arrays.toString(parameterTypes) + " matching the "
                    + WRITER_INTERFACE + " contract. A Target is driven through that contract, so"
                    + " a writer missing part of it cannot be deployed either.", e);
        }
    }

    /**
     * Whether the writer carries the {@code Acknowledgeable} marker, and so was given a callback.
     *
     * <p>Reported rather than assumed. Without the marker the platform never calls a writer back,
     * so it silently loses whatever it had not committed when the checkpoint advanced — and in
     * this tier it would surface only as an ack count of zero, which is also what the legitimate
     * no-recovery path produces. Naming it is what separates the two.</p>
     */
    boolean isAcknowledgeable() {
        return acknowledgeable;
    }

    private Object invoke(Method method, String name, Object... args) throws Exception {
        if (instance == null) {
            throw new IllegalStateException("the target is closed; " + name + " has nothing to call");
        }
        try {
            return method.invoke(instance, args);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw new IllegalStateException(writerClass.getName() + "." + name + " threw", cause);
        }
    }
}
