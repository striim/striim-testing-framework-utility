package com.example.common;

import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.uuid.UUID;

import java.io.Flushable;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * Owns the whole state lifecycle for one OP core: restore on start, dirty-gated persist on an
 * interval, persist on flush, persist on close. The ONE place the cadence lives.
 *
 * <p>Honors {@code EnableMdrRecovery} and {@code MdrCheckpointIntervalMs} by those names, and
 * reads the component UUID off {@link AbstractConvertingOpenProcessorApp#COMPONENT_UUID_KEY}, so
 * a consumer needs no new plumbing. An Open Processor cannot join the platform's periodic
 * checkpoint; these are its only reachable durable-state moments.</p>
 *
 * <p><b>Reconciliation is the consumer's, not this class's.</b> A consumer with an authoritative
 * external record of its own progress MUST reconcile the restored state against that record in
 * {@code start()}, before the state reaches any decision path. MDR state is a cache: where the
 * external record can answer, it wins; where it cannot, the restored value must be clamped so it
 * cannot exceed what the external record does know.</p>
 *
 * @param <S> the module's state POJO — plain getters/setters, no {@code @JsonTypeInfo}
 */
public final class ComponentStateCheckpointer<S> implements Flushable, AutoCloseable {

    /** Recovery on/off property; default {@code true}. */
    public static final String ENABLED_PROPERTY = "EnableMdrRecovery";

    /** Persist interval in ms; {@code 0} persists only on flush/close; default {@code 5000}. */
    public static final String INTERVAL_MS_PROPERTY = "MdrCheckpointIntervalMs";

    private static final int DEFAULT_INTERVAL_MS = 5000;

    private final Class<S> stateClass;
    private final Supplier<S> initial;
    private final DirtyState dirty;
    private final ComponentStateStore store;
    private final Logger logger;
    private final UUID componentUuid;
    private final boolean enabled;
    private final long intervalMs;

    private S state;
    private Supplier<S> snapshot;
    private ScheduledExecutorService scheduler;
    private ScheduledFuture<?> task;

    private ComponentStateCheckpointer(
            Class<S> stateClass, Supplier<S> initial, DirtyState dirty, ComponentStateStore store,
            Logger logger, UUID componentUuid, boolean recoveryRequested, long intervalMs) {
        this.stateClass = stateClass;
        this.initial = initial;
        this.dirty = dirty;
        this.store = store;
        this.logger = logger;
        this.componentUuid = componentUuid;
        boolean usable = recoveryRequested && componentUuid != null;
        if (usable && store == null) {
            usable = false;
            if (logger != null) {
                logger.logWarn(() -> "No component state store available; MDR recovery disabled"
                        + " for component " + componentUuid);
            }
        }
        this.enabled = usable;
        this.intervalMs = intervalMs;
    }

    /**
     * Builds the checkpointer from the props map the shared App base already enriches.
     * Rejects a negative or non-integer interval with an {@link IllegalArgumentException} naming
     * the property, before any store access.
     */
    public static <S> ComponentStateCheckpointer<S> fromProps(
            Map<String, Object> props, Class<S> stateClass, Supplier<S> initial,
            DirtyState dirty, ComponentStateStore store, Logger logger) {
        Object uuid = (props == null) ? null : props.get(
                AbstractConvertingOpenProcessorApp.COMPONENT_UUID_KEY);
        boolean recoveryRequested = Boolean.parseBoolean(
                optionalString(props, ENABLED_PROPERTY, "true"));
        long intervalMs = nonNegativeInt(props, INTERVAL_MS_PROPERTY, DEFAULT_INTERVAL_MS);
        return new ComponentStateCheckpointer<S>(stateClass, initial, dirty, store, logger,
                (uuid instanceof UUID) ? (UUID) uuid : null, recoveryRequested, intervalMs);
    }

    /**
     * Returns the state persisted by a previous run of this component, or a fresh initial state.
     * Starts the interval timer when enabled. The consumer reconciles the returned value before
     * any decision path reads it.
     */
    public synchronized S restoreOrInitial() {
        if (enabled) {
            try {
                S restored = MdrPositionCarrier.extractState(store.get(componentUuid), stateClass);
                if (restored != null) {
                    state = restored;
                    startTimer();
                    return state;
                }
            } catch (Throwable t) {
                if (logger != null) {
                    logger.logWarn(() -> "Failed to restore component state from MDR: " + t);
                }
            }
        }
        state = (initial == null) ? null : initial.get();
        startTimer();
        return state;
    }

    /**
     * Adopts the post-reconciliation state object as the one this checkpointer persists.
     * A consumer that replaces the restored object during reconciliation MUST call this before
     * any event mutates state, or flush/close would persist the pre-reconciliation object.
     */
    public synchronized void adopt(S reconciled) {
        if (reconciled == null) {
            throw new IllegalArgumentException("Cannot adopt a null reconciled state");
        }
        state = reconciled;
    }

    /**
     * Makes every persist write {@code supplier.get()} instead of the long-lived state object.
     *
     * <p>For a consumer whose durable state is a SNAPSHOT of live structures — a buffer of held
     * events, say — rather than a POJO it mutates in place. The {@code dirty} gate given at
     * construction decides whether a persist is due, and its {@code markClean()} runs after the
     * snapshot was written, so a gate that versions its structure can record exactly which
     * snapshot is durable. {@link #restoreOrInitial} is unaffected: the restored object is
     * returned to the consumer to load into its live structures, and never written back.</p>
     */
    public synchronized void snapshotWith(Supplier<S> supplier) {
        this.snapshot = supplier;
    }

    /** Persists the current state when dirty; marks it clean only after a successful write. */
    public synchronized boolean persistIfDirty() {
        DirtyState gate = (snapshot == null && state instanceof DirtyState) ? (DirtyState) state : dirty;
        if (!enabled || gate == null || !gate.isDirty()) {
            return false;
        }
        S current = (snapshot != null) ? snapshot.get() : state;
        if (current == null) {
            return false;
        }
        try {
            ComponentCheckpoint checkpoint =
                    MdrPositionCarrier.toStateCheckpoint(current, componentUuid);
            if (checkpoint != null && store.put(checkpoint)) {
                gate.markClean();
                return true;
            }
        } catch (Throwable t) {
            if (logger != null) {
                logger.logWarn(() -> "Failed to persist component state to MDR: " + t);
            }
        }
        return false;
    }

    /** Quiesce-flush hook: persists any dirty state. Never throws. */
    @Override
    public void flush() {
        persistIfDirty();
    }

    /** Cancels the timer and performs a final dirty-gated persist. */
    @Override
    public synchronized void close() {
        if (scheduler != null) {
            ScheduledFuture<?> pending = task;
            if (pending != null) {
                pending.cancel(false);
            }
            scheduler.shutdownNow();
            scheduler = null;
            task = null;
        }
        persistIfDirty();
    }

    /** True when recovery is enabled AND this component has a UUID to key its state by. */
    public boolean enabled() {
        return enabled;
    }

    /** The component UUID this state is keyed by; null when absent from props. */
    public UUID componentUuid() {
        return componentUuid;
    }

    private void startTimer() {
        if (!enabled || intervalMs <= 0 || scheduler != null) {
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "ComponentStateCheckpointer-" + stateClass.getSimpleName());
            t.setDaemon(true);
            return t;
        });
        task = scheduler.scheduleWithFixedDelay(
                this::persistIfDirty, intervalMs, intervalMs, TimeUnit.MILLISECONDS);
    }

    private static String optionalString(Map<String, Object> props, String key, String defaultValue) {
        Object raw = (props == null) ? null : props.get(key);
        if (raw == null) {
            return defaultValue;
        }
        String text = raw.toString().trim();
        return text.isEmpty() ? defaultValue : text;
    }

    private static long nonNegativeInt(Map<String, Object> props, String key, int defaultValue) {
        String value = optionalString(props, key, Integer.toString(defaultValue));
        final long parsed;
        try {
            parsed = Long.parseLong(value);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(key + " must be an integer, got: " + value, e);
        }
        if (parsed < 0) {
            throw new IllegalArgumentException(key + " must be zero or positive, got: " + value);
        }
        return parsed;
    }
}
