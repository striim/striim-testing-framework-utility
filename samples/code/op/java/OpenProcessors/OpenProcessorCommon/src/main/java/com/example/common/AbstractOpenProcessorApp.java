package com.example.common;

import java.util.List;
import java.util.Map;

import com.example.common.SyntheticTransactionBoundaries.OnUnsafeRecovery;
import com.example.common.SyntheticTransactionBoundaries.Positioned;
import com.example.common.SyntheticTransactionBoundaries.Settings;
import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.runtime.components.openprocessor.OpenProcessor;

/**
 * Shared humble-shell base for near-identical {@code App} classes: the
 * {@code start()}/{@code run()}/{@code close()} boilerplate — drain {@code getAdded()}, filter each
 * entry through {@code accepts}, unwrap the container {@code WAEvent}, {@code processEvent},
 * {@code send}, {@code close} — delegating all transform logic to the subclass's
 * {@link EventProcessor}.
 *
 * <p><b>This class is now {@link AbstractConvertingOpenProcessorApp} bound to {@code WAEvent}</b>,
 * and holds no loop of its own. The split exists because a format CONVERTER consumes one event kind
 * and emits another — reading an {@code AvroEvent}, say — which
 * {@code EventProcessor<E>} and a hard {@code (WAEvent) container.data} cast cannot express.
 * Everything an OP on this base sees is unchanged: the same reserved keys, the same
 * {@code buildProcessor} hook, the same {@code accepts}/{@code failBatchOnEventError} overrides,
 * the same {@code emitAsync}. <b>None of the eight subclasses changed when the split landed</b> —
 * their {@code EventProcessor<WAEvent> buildProcessor(...)} is a covariant override of the
 * inherited {@code ConvertingEventProcessor<WAEvent, WAEvent>} form.</p>
 *
 * <p>Keeping this shell humble is what lets each OP's core be unit tested without a live Striim
 * runtime: the subclass implements only {@link #buildProcessor}, and every other lifecycle method
 * lives one level up, once.</p>
 */
public abstract class AbstractOpenProcessorApp extends AbstractConvertingOpenProcessorApp<WAEvent> {

    /**
     * Wraps the OP's output in synthetic {@code BEGIN}/{@code COMMIT} — see
     * {@link SyntheticTransactionBoundaries}. Null unless {@link #transactionSettings} named a
     * scope, so a non-opting OP pays one null check per event.
     *
     * <p>Assigned in {@link #start()}, read in {@link #beforeSend}: the same publication pattern as
     * {@code processor} and {@code logger}, and safe for the same reason.</p>
     */
    private SyntheticTransactionBoundaries transactionBoundaries;

    /** The settings in force; {@link Settings#OFF} unless {@link #transactionSettings} said otherwise. */
    private Settings transactions = Settings.OFF;

    /**
     * {@inheritDoc}
     *
     * <p>{@code final} here: an OP on this base consumes {@code WAEvent} by definition, so the
     * question does not arise for it. A core reading any other kind extends
     * {@link AbstractConvertingOpenProcessorApp} directly and answers it there.</p>
     */
    @Override
    protected final Class<WAEvent> inputType() {
        return WAEvent.class;
    }

    /**
     * The synthetic transaction settings — {@value SyntheticTransactionBoundaries#SCOPE_PROPERTY}
     * ({@code none} | {@code event} | {@code batch}), its deprecated alias
     * {@value SyntheticTransactionBoundaries#PER_SOURCE_EVENT_PROPERTY},
     * {@value SyntheticTransactionBoundaries#GROUP_BY_TABLE_PROPERTY} and
     * {@value SyntheticTransactionBoundaries#ON_UNSAFE_RECOVERY_PROPERTY}. Default
     * {@link Settings#OFF}: an OP that has not declared the properties in its
     * {@code @PropertyTemplate} never wraps, however the flow is configured. An OP that declares
     * them returns {@link Settings#fromProps}, whose refusals ({@code TransactionBatchSize}, a
     * disagreeing alias, grouping outside batch scope) surface at {@link #start()}.
     *
     * <p><b>Deliberately NOT gated on recovery.</b> An earlier revision emitted nothing when the
     * flow had no recovery, on the reasoning that markers only matter for replay. That left a
     * writer configured for them — commit only on source boundaries, on a boundary-less stream —
     * with no commit trigger at all the moment recovery was switched off: its count and timer
     * commits are disabled, and a group commit policy's per-add counter never resets without a
     * {@code COMMIT}. Whether the markers are
     * NEEDED depends on recovery; whether the writer is WAITING for them does not, so the property
     * alone decides. {@link #recoveryEnabled()} is logged at start so the operator can tell which
     * case they are in.</p>
     *
     * <p><b>Grouping under recovery warns, or halts.</b> A grouped release is not monotone in
     * position and the endpoint's replay filter retires on the first accepted event
     * so it is sound only under conditions the OP cannot
     * verify (see {@link SyntheticTransactionBoundaries}). {@code OnUnsafeRecovery: 'warn'} (the
     * default) logs them once at start and runs; {@code 'halt'} refuses.</p>
     */
    protected Settings transactionSettings(Map<String, Object> props) {
        return Settings.OFF;
    }

    /**
     * The tables a grouped release ({@value SyntheticTransactionBoundaries#GROUP_BY_TABLE_PROPERTY})
     * puts first, in this order — for a mapping OP, its config's target order, so the release
     * order does not depend on which target the first event of a batch happened to produce.
     * Tables not named follow in first-appearance order. Read once, after {@link #buildProcessor},
     * so it may consult the built processor. Default: none.
     */
    protected List<String> transactionBatchTableOrder() {
        return List.of();
    }

    /**
     * Whether the owning flow was deployed with recovery — {@code FlowComponent.isRecoveryEnabled()}
     * on the platform handle, which {@code Flow} sets on every component it creates from
     * {@code MetaInfo.Flow.recoveryType}. False before
     * {@code setOpenProcessor()}.
     */
    protected boolean recoveryEnabled() {
        OpenProcessor component = getBaseOpenProcessor();
        return component != null && component.isRecoveryEnabled();
    }

    /** Why grouping under recovery is unsound unless the whole application is arranged for it. */
    static String unsafeRecoveryConditions(String component) {
        return component + ": " + SyntheticTransactionBoundaries.GROUP_BY_TABLE_PROPERTY + " under RECOVERY"
                + " releases each batch table by table, which is not monotone in position. The platform's"
                + " replay filter drops a replayed prefix and retires at the first accepted event, so a"
                + " replayed batch whose boundaries differ from the original applies the later tables'"
                + " already-committed rows again, and a target that commits inside a batch loses the rest of"
                + " that table's run on a STOP. This is sound only if ALL of the following hold, none of"
                + " which this OP can verify: (1) every target on this application commits and acknowledges"
                + " only on this OP's BEGIN/COMMIT markers -- set each target to preserve source transaction"
                + " boundaries and disable its own count and interval commits; (2) no CQ between this OP and"
                + " any target filters BEGIN/COMMIT; (3) the window feeding this OP is count-only (KEEP n"
                + " ROWS with no WITHIN), so a replay rebuilds the same batches -- a time-bounded window can"
                + " duplicate rows in keyless tables after a restart; (4) every other target on this"
                + " application also acknowledges only at batch boundaries, since one that does not moves"
                + " the replay start into a batch for all of them.";
    }

    @Override
    public void start() throws Exception {
        super.start(); // the platform populates getProperties() here
        Map<String, Object> props = getProperties();
        Settings requested;
        try {
            requested = transactionSettings(props);
        } catch (RuntimeException malformed) {
            closeProcessorQuietly(); // a refused OP holds nothing open
            throw malformed;
        }
        if (!requested.enabled()) {
            return;
        }
        final String component = componentQualifiedName();
        final boolean recovery = recoveryEnabled();
        if (requested.groupByTable() && recovery) {
            if (requested.onUnsafeRecovery() == OnUnsafeRecovery.HALT) {
                closeProcessorQuietly();
                throw new IllegalArgumentException(unsafeRecoveryConditions(component) + " Refused:"
                        + " " + SyntheticTransactionBoundaries.ON_UNSAFE_RECOVERY_PROPERTY + " is 'halt'. Deploy"
                        + " without RECOVERY, leave " + SyntheticTransactionBoundaries.GROUP_BY_TABLE_PROPERTY
                        + " off, or set " + SyntheticTransactionBoundaries.ON_UNSAFE_RECOVERY_PROPERTY
                        + ": 'warn' once the conditions are met.");
            }
            logger.logAlways(() -> unsafeRecoveryConditions(component) + " Set "
                    + SyntheticTransactionBoundaries.ON_UNSAFE_RECOVERY_PROPERTY + ": 'halt' to refuse instead.");
        }
        if (requested.viaDeprecatedAlias()) {
            logger.logAlways(() -> SyntheticTransactionBoundaries.PER_SOURCE_EVENT_PROPERTY + " is deprecated:"
                    + " set " + SyntheticTransactionBoundaries.SCOPE_PROPERTY + ": 'event' instead.");
        }
        transactions = requested;
        transactionBoundaries = new SyntheticTransactionBoundaries(component, logger, requested.groupByTable(),
                transactionBatchTableOrder());
        if (requested.batchScoped()) {
            fanOutRecoveryWarning().noteBoundaries(); // every released batch is framed
        }
        final String scope = requested.batchScoped()
                ? " Each synthetic transaction spans one platform batch (a window's chunk; one event behind a"
                        + " bare CQ)" + (requested.groupByTable() ? ", released table by table" : "") + "."
                : " Each synthetic transaction spans one source event's output.";
        logger.logAlways(() -> SyntheticTransactionBoundaries.SCOPE_PROPERTY + " is '"
                + requested.scope().name().toLowerCase(java.util.Locale.ROOT) + "'; recovery is "
                + (recovery ? "ON, so the markers protect replay" : "OFF, so the markers only give the"
                        + " writer its commit points") + ". Wrapping starts with the first source event"
                + " unless the input carries transaction boundaries of its own." + scope);
    }

    /**
     * {@inheritDoc}
     *
     * <p>Applies {@link SyntheticTransactionBoundaries} at event scope; at batch scope the
     * wrapping happens at the release, in {@link #emitGroup}.</p>
     */
    @Override
    protected List<WAEvent> beforeSend(WAEvent source, List<WAEvent> emitted) {
        SyntheticTransactionBoundaries boundaries = transactionBoundaries;
        return boundaries == null || transactions.batchScoped() ? emitted : boundaries.apply(source, emitted);
    }

    /**
     * {@inheritDoc}
     *
     * <p>At batch scope the group is handed to {@link SyntheticTransactionBoundaries#absorb}
     * instead: nothing is sent while the platform batch is being processed, and the whole batch
     * goes out as ONE send with each member carrying its own source position at the end of this
     * {@code run()}. The fan-out diagnostic sees the group as emitted, with the boundaries noted
     * at start.</p>
     */
    @Override
    protected void emitGroup(WAEvent source, List<WAEvent> outEvents, ImmutableStemma stemma) throws Exception {
        if (!transactions.batchScoped()) {
            super.emitGroup(source, outEvents, stemma);
            return;
        }
        try {
            fanOutRecoveryWarning().observe(outEvents, stemma, logger);
        } catch (RuntimeException diagnosticFailure) {
            // as in AbstractConvertingOpenProcessorApp.emitGroup: a diagnostic may not fail the batch
        }
        sendReleased(transactionBoundaries.absorb(source, stemma, outEvents));
    }

    /**
     * {@inheritDoc}
     *
     * <p>After the batch: the open synthetic transaction is released and sent, so nothing is held
     * past this {@code run()} (an OP that holds events across {@code run()} is invisible to the
     * checkpoint marker). Before the batch: anything still held was
     * absorbed by a {@code run()} that threw under the fail-the-batch error model — it is
     * discarded, not sent, or the replay of that input would apply it twice. Both calls also reset
     * the batch's counters, so a run whose sources all produced nothing leaves no stale first
     * source behind.</p>
     */
    @Override
    protected void releaseTransactionBatchAtBoundary(boolean afterBatch) throws Exception {
        SyntheticTransactionBoundaries boundaries = transactionBoundaries;
        if (boundaries == null || !transactions.batchScoped()) {
            return;
        }
        if (afterBatch) {
            sendReleased(boundaries.release());
            return;
        }
        releaseFailure = null; // a new run: the previous run's verdict no longer applies
        final int dropped = boundaries.discard();
        if (dropped > 0) {
            logger.logWarn(() -> dropped + " event(s) held by a run() that failed were discarded ahead of the"
                    + " platform's replay of that batch, so they are absorbed once, not twice");
        }
    }

    /**
     * {@inheritDoc}
     *
     * <p>At batch scope the original joins the open batch in its place, so it neither lands bare
     * between two synthetic transactions nor overtakes the outputs held ahead of it.</p>
     */
    @Override
    protected void passThroughOnError(WAEvent source, ImmutableStemma stemma) throws Exception {
        if (source == null) {
            return;
        }
        if (transactions.batchScoped()) {
            sendReleased(transactionBoundaries.absorb(source, stemma, List.of(source)));
            return;
        }
        PositionPropagation.sendAllWithPosition(this, beforeSend(source, List.of(source)), stemma);
    }

    /**
     * The exception of a send that failed at a batch's release, until the next run. A release
     * failure fails the platform batch under every error model: the released events were several
     * source events' outputs, and the best-effort passthrough of ONE source cannot stand in for
     * them. Nothing reached the stream (one {@code TaskEvent}, all or nothing). The exception is
     * rethrown AS IS — the platform classifies a failure by its class name and never unwraps
     * so wrapping it would turn every release failure into an
     * unclassified crash — and recognised here by identity.
     */
    private Exception releaseFailure;

    /** {@inheritDoc} A release failure fails the batch under every error model. */
    @Override
    protected boolean failBatchOnEventError(Exception failure) {
        return (failure != null && failure == releaseFailure) || super.failBatchOnEventError(failure);
    }

    private void sendReleased(List<Positioned> released) throws Exception {
        if (released.isEmpty()) {
            return;
        }
        final int n = released.size();
        logger.log(() -> "Sending " + n + " event(s)");
        try {
            PositionPropagation.sendAllWithPositions(this, released);
        } catch (Exception e) {
            releaseFailure = e;
            throw e;
        }
    }

    private void closeProcessorQuietly() {
        try {
            if (processor != null) {
                processor.close();
            }
        } catch (Exception ignore) {
            // the refusal is the failure being reported
        }
    }

    /** A partial batch is released, never held across a quiesce. */
    @Override
    public void flush() throws Exception {
        releasePending();
        super.flush();
    }

    /**
     * {@inheritDoc}
     *
     * <p>A partial batch is released first, best effort — normally there is none, since every
     * {@code run()} releases its own; if the send fails nothing of it reached the stream.</p>
     */
    @Override
    public void close() throws Exception {
        try {
            releasePending();
        } catch (Exception e) {
            if (logger != null) {
                logger.logError(() -> "Releasing the open synthetic transaction batch at close failed: " + e);
            }
        }
        super.close();
    }

    private void releasePending() throws Exception {
        SyntheticTransactionBoundaries boundaries = transactionBoundaries;
        if (boundaries == null || !transactions.batchScoped()) {
            return;
        }
        sendReleased(boundaries.release());
    }

    /**
     * {@inheritDoc}
     *
     * <p>Redeclared at the narrower {@link EventProcessor} return so a subclass's own
     * {@code EventProcessor<WAEvent>} signature keeps reading as the natural override it has always
     * been, rather than as a covariant narrowing of a converting form it never uses.</p>
     */
    @Override
    protected abstract EventProcessor<WAEvent> buildProcessor(Map<String, Object> props) throws Exception;
}
