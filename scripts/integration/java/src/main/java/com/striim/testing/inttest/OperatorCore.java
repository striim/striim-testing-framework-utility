package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * Drives an IN-STREAM op: the {@code processEvent(WAEvent)} contract (constructor injection below),
 * shared by {@link IntegrationProcessor#drive} and {@link PerformanceProcessor}
 * (PERF_SPEC.md) so neither duplicates the reflection logic. The jar-load/construct/
 * {@code start()} sequence that precedes it lives in {@link LoadedOp}, which {@link SourceCore}
 * shares.
 *
 * <p>
 * A built {@code OperatorCore} wraps one already-constructed, already-{@code start()}ed
 * core instance plus its resolved {@code processEvent}/{@code close} {@link Method}
 * handles. {@link #processEvent} and {@link #close} are the only two operations a
 * caller performs on it afterward; both preserve {@link IntegrationProcessor#drive}'s
 * original error-message wording exactly, so the existing JUnit suite (which drives
 * through {@code IntegrationProcessor.drive}, not this class directly) keeps passing
 * unchanged.
 */
final class OperatorCore implements EventDriver {

    private final String coreClassName;
    private final Object instance;
    private final Method processEvent;
    private final Method close; // nullable: no close() lifecycle declared

    private OperatorCore(String coreClassName, Object instance, Method processEvent, Method close) {
        this.coreClassName = coreClassName;
        this.instance = instance;
        this.processEvent = processEvent;
        this.close = close;
    }

    /**
     * Loads and constructs the core via {@link LoadedOp#load} (the shared jar-load/construct/
     * {@code start()} sequence), then resolves the {@code processEvent(WAEvent)} contract every
     * in-stream op satisfies.
     */
    static OperatorCore build(File opJar, Map<String, Object> properties, Map<String, Object> types, List<? extends Event> typeStampingInput,
            List<String> passwordProperties, String namespace, String sourceName) throws Exception {
        LoadedOp loaded = LoadedOp.load(opJar, properties, types, typeStampingInput, passwordProperties, namespace, sourceName);

        return new OperatorCore(loaded.coreClassName, loaded.instance,
                resolveProcessEvent(loaded.coreClass, loaded.coreClassName), loaded.optionalClose());
    }

    /**
     * The core's {@code processEvent} handle.
     *
     * <p>{@code processEvent(WAEvent)} FIRST, and by exact match: it is the {@code EventProcessor}
     * contract every in-stream op satisfies, and a core implementing
     * {@code EventProcessor<WAEvent>} also carries javac's synthetic bridge
     * {@code processEvent(Event)} — so a widest-match search would be free to pick the bridge, and
     * a core that declared both a real {@code Object} overload and the bridge would resolve
     * unpredictably between runs, since {@code getMethods()} has no defined order.</p>
     *
     * <p>Only when there is no such method does this fall back to the single one-argument
     * {@code processEvent} the core declares — which is how an operator converting a wire format is
     * driven ({@code Processor.processEvent(AvroEvent)}, AvroConverterOp). <b>Declared, not
     * inherited</b>, and <b>exactly one</b>: two overloads are rejected by name rather than guessed
     * between, because picking the wrong one would feed the operator through a path its author did
     * not mean and the case would assert against it happily. {@code isSynthetic} is what keeps the
     * javac bridge a converting core carries — {@code processEvent(Event)}, from implementing
     * {@code ConvertingEventProcessor} — out of the count and out of the selection.</p>
     *
     * <p>⚠ <b>A core taking {@code Object} cannot be pre-checked</b>: every event is assignable to
     * it, so {@link #processEvent}'s guard below has nothing to compare against and only the
     * operator's own cast reports a fixture of the wrong kind. No module is in that shape today —
     * AvroConverterOp was the one, until slice {@code 75c} narrowed it to {@code AvroEvent} — but
     * the guard is a function of what the core declares, not a universal safety net.</p>
     */
    private static Method resolveProcessEvent(Class<?> coreClass, String coreClassName) {
        try {
            return coreClass.getMethod("processEvent", WAEvent.class);
        } catch (NoSuchMethodException expected) {
            // Not the WAEvent contract; fall through to the single-parameter search below.
        }

        // PUBLIC and declared: the fallback drives a contract, and a core's private helper that
        // happens to be named processEvent is not one. getDeclaredMethods alone would have made
        // it selectable, and setAccessible below would then have driven it.
        List<Method> candidates = new ArrayList<>();
        for (Method method : coreClass.getDeclaredMethods()) {
            if (method.getName().equals("processEvent") && method.getParameterCount() == 1
                    && !method.isSynthetic() && java.lang.reflect.Modifier.isPublic(method.getModifiers())) {
                candidates.add(method);
            }
        }
        if (candidates.isEmpty()) {
            throw new IllegalStateException(coreClassName + " has no processEvent(WAEvent) method"
                    + " matching the EventProcessor contract, and no single-argument processEvent"
                    + " to drive instead");
        }
        if (candidates.size() > 1) {
            throw new IllegalStateException(coreClassName + " declares " + candidates.size()
                    + " single-argument processEvent methods " + candidates
                    + "; the harness will not guess which one a case means. Leave exactly one, or"
                    + " declare processEvent(WAEvent) so the EventProcessor contract selects it.");
        }
        Method only = candidates.get(0);
        only.setAccessible(true);
        return only;
    }

    /**
     * Invokes {@code processEvent(event)} and returns the emitted events (empty, never
     * {@code null}, when the core itself returns {@code null} — docs/INTEGRATION-TESTS.md's "no
     * emission" case).
     */
    @Override
    @SuppressWarnings("unchecked")
    public List<WAEvent> processEvent(Event event) throws Exception {
        Class<?> parameter = processEvent.getParameterTypes()[0];
        if (event != null && !parameter.isInstance(event)) {
            // Caught here rather than as a reflective IllegalArgumentException, which names only
            // "argument type mismatch". The usual cause is a fixture `kind` that does not match
            // the operator -- a waevent fixture aimed at AvroConverterOp, or the reverse.
            throw new IllegalArgumentException(coreClassName + ".processEvent takes a "
                    + parameter.getName() + ", but the fixture supplied a "
                    + event.getClass().getName() + "; check the fixture's `kind`.");
        }
        Object result;
        try {
            result = processEvent.invoke(instance, event);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException(coreClassName + ".processEvent threw for input event " + event, e.getCause() != null ? e.getCause() : e);
        }
        if (result == null) {
            return List.of();
        }
        if (!(result instanceof List<?> emitted)) {
            throw new IllegalStateException(coreClassName + ".processEvent returned a non-List result: " + result.getClass());
        }
        List<WAEvent> output = new ArrayList<>(emitted.size());
        for (Object o : emitted) {
            output.add((WAEvent) o);
        }
        return output;
    }

    /** The driven core instance, for {@link JmxSnapshot}. */
    Object instance() {
        return instance;
    }

    /** Invokes {@code close()} if the core declared one; a no-op otherwise. */
    @Override
    public void close() throws Exception {
        if (close == null) {
            return;
        }
        try {
            close.invoke(instance);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException(coreClassName + ".close() threw", e.getCause() != null ? e.getCause() : e);
        }
    }
}
