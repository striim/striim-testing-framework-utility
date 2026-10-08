package com.webaction.proc;

import java.util.Map;

import com.webaction.event.Event;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.Position;
import com.webaction.runtime.components.ReceiptCallback;
import com.webaction.runtime.monitor.MonitorEventsCollection;
import com.webaction.source.lib.prop.Property;
import com.webaction.source.lib.prop.RetryPolicy;
import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.proc.RetriableWriter} — the base every field-built
 * Striim Target extends, through {@code common.AbstractWriterApp}.
 *
 * <h2>What this mock is for, and what it is not</h2>
 * <b>stubbing this class means the tier
 * tests a writer against OUR MODEL of the lifecycle, not against the platform's.</b> Proving
 * platform integration is T4's job, and it cannot be
 * delegated to a mock — a writer that passed 551 unit tests reported RUNNING for 240 seconds while
 * dead, and nothing below the live tier could see it.
 *
 * <p>What the tier gets in exchange is the <b>real database</b>: a real driver, real metadata, real
 * type conversion, in-process and cheap enough to fork per time zone. Every metadata trap this
 * writer has found — {@code getIndexInfo}'s TYPE read as a statistics row, {@code DELETE_RULE}
 * defaulting to CASCADE, {@code KEY_SEQ} bases, unnamed foreign-key grouping — is otherwise proven
 * only against a dynamic proxy written by the same hand that wrote the code it checks.</p>
 *
 * <h2>The reductions, named</h2>
 * <ul>
 *   <li>The real class extends {@code BaseWriter extends BaseProcess}, which drag in formatters,
 *       metering and the MDR. The hierarchy is collapsed to this one class, exactly as the
 *       harness's {@code WAEvent} collapses its own.</li>
 *   <li>{@code receive} and {@code retryTransaction}, the RETRY machinery this class is named
 *       for, are absent, and {@code onConnectionException} rethrows instead of reconnecting. A
 *       tier case reaches {@code processEvent} directly, so a writer's retry behaviour is NOT
 *       exercised here.</li>
 *   <li>{@link #init} does not read {@code formatterProperties}; a Target that writes through JDBC
 *       has no formatter.</li>
 * </ul>
 *
 * <p>Members match the platform class's public signatures on 5.4.</p>
 */
public abstract class RetriableWriter {

    /** Set by {@link #close()}; a writer polls it to stop work in flight. */
    protected volatile boolean isClosed;

    /**
     * The monitor {@code receive} holds while calling {@code processEvent}.
     *
     * <p>Load-bearing rather than decorative: {@code AbstractWriterApp}'s age-deadline timer locks
     * the same object, so without it the timer could drain the accumulator while an arriving event
     * is being added to it.</p>
     */
    protected Object synchObject = new Object();

    /** True while the platform is retrying a failed transaction. Never set by this tier. */
    protected boolean inRetryLoop;

    /** The application's RECOVERY setting; {@code TargetCore} sets it from the case before init. */
    protected boolean isRecoveryEnabled;

    /** The {@code Tables} property's name, as the platform passes it down. */
    protected String tableMapProp;

    /**
     * The in-flight window replayed on reconnect.
     *
     * <p><b>Left null by this tier</b>, which is what {@code AbstractWriterApp.ackBatch}
     * null-guards for — the platform fills it inside {@code receive}, above the seam
     * {@code TargetCore} drives.</p>
     */
    protected WriterEventBuffer eventBuffer;

    /**
     * How a Target releases the checkpoint pin on durable data.
     *
     * <p><b>Genuinely nullable, and measured so.</b> The platform injects it only when the adapter
     * implements {@code Acknowledgeable}; a writer without the marker is never called back.
     * {@code TargetCore} reproduces that gate rather than always injecting, so a sample writer that
     * drops the marker fails here the way it would in production.</p>
     */
    protected ReceiptCallback receiptCallback;

    /** Base initialisation. Subclasses override and call {@code super.init(...)} first. */
    public void init(Map<String, Object> properties, Map<String, Object> formatterProperties,
            UUID inputStream, String distributionID) throws Exception {
        this.tableMapProp = properties == null ? null : (String) properties.get("Tables");
    }

    /** The platform calls this before {@link #init}. */
    public void setRecoveryEnabled(boolean isRecoveryEnabled) {
        this.isRecoveryEnabled = isRecoveryEnabled;
    }

    /** Injects the acknowledgement seam. */
    public void setReceiptCallback(ReceiptCallback receiptCallback) {
        this.receiptCallback = receiptCallback;
    }

    /**
     * Base shutdown.
     *
     * <p>Only flips the flags — it does NOT delegate to {@link #cleanup()}. That is the platform's
     * real behaviour and the reason {@code AbstractWriterApp} overrides {@code close()} to call
     * {@code cleanup()} itself; reproducing the omission is what lets this tier catch a writer that
     * leaks its connection on undeploy.</p>
     */
    public void close() throws Exception {
        isClosed = true;
        inRetryLoop = false;
    }

    /**
     * The platform's flush hook.
     *
     * <p>{@code BaseProcess.flush()} takes no action; the built-in {@code DatabaseWriter} and
     * {@code AbstractWriterApp} both override it to drain their batch.</p>
     */
    public void flush() throws Exception {
    }

    /** The position the target has made durable. Overridden by every writer that recovers. */
    public Position getEndpointCheckpoint() throws Exception {
        return null;
    }

    /** Publishes this component's metrics. Subclasses call {@code super} then add their own. */
    public void publishMonitorEvents(MonitorEventsCollection events) {
    }

    /** How many events this writer discarded rather than wrote. */
    public long getDiscardedEventCount() {
        return 0L;
    }

    /** The platform reconnects here. This tier cannot, so the loss propagates. */
    public void onConnectionException(Exception exception) throws Exception {
        throw exception;
    }

    /**
     * The platform transitions the application here. This tier does not model that; the throw
     * keeps the failure visible to the caller.
     */
    protected void notifyException(Exception exception) {
        throw new UnsupportedOperationException(
                "notifyException is not modelled by the integration tier", exception);
    }

    /** Typed (non-WAEvent) input is never driven by this tier. */
    protected Event convertTypedeventToWAevent(Event event) {
        throw new UnsupportedOperationException(
                "typed events are not driven by the integration tier");
    }

    /** One event, with the recovery position that pins it. */
    public abstract void processEvent(int channel, Event event, ImmutableStemma position)
            throws Exception;

    /** One event, with no position — the no-recovery path. */
    public abstract void processEvent(int channel, Event event) throws Exception;

    /** Releases the writer's own resources. Nothing in the platform calls this; see {@link #close}. */
    public abstract void cleanup() throws Exception;

    /** True when {@code event} should be discarded rather than written. */
    public abstract boolean isDiscarded(Event event);

    /** The wait-and-attempt policy to use when the target is unreachable. */
    public abstract RetryPolicy getRetryPolicy(Property property);

    /**
     * The real accessor is {@code BaseProcess}'s. Left null by this tier: a writer built here is
     * not attached to a flow, and {@code common.ExceptionStoreNotifier} sends nothing for a null
     * component -- which is exactly the platform's own "outside a flow" answer. Present so the
     * writer's skip path resolves instead of failing with
     * {@code NoSuchMethodError} the first time a row is skipped.
     */
    protected com.webaction.runtime.components.FlowComponent flowComponent;

    public com.webaction.runtime.components.FlowComponent getFlowComponent() {
        return flowComponent;
    }

    public void setFlowComponent(final com.webaction.runtime.components.FlowComponent component) {
        this.flowComponent = component;
    }
}
