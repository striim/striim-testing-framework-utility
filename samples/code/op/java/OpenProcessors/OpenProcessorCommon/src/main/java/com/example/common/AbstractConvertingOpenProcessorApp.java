package com.example.common;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.Objects;

import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.runtime.components.Flow;
import com.webaction.runtime.components.openprocessor.OpenProcessor;
import com.webaction.runtime.components.openprocessor.StriimOpenProcessor;
import com.webaction.runtime.containers.IBatch;
import com.webaction.runtime.meta.MetaInfo;

/**
 * The drain loop, generic in the INPUT event kind. Most OPs consume and emit the same kind and
 * extend {@link AbstractOpenProcessorApp}, which is this class bound to {@code WAEvent}; a format
 * CONVERTER — one that reads an {@code AvroEvent}, say — extends this one directly.
 *
 * <p><b>Only the input is a parameter.</b> The output is fixed at {@code proc.events.WAEvent}
 * because {@link PositionPropagation#sendWithPosition} is: the platform's send takes that type, so
 * an app shell cannot emit anything else whatever its core produces. Making {@code O} a parameter
 * here would promise a generality the send path cannot honour.</p>
 *
 * <p>Shared humble-shell base for near-identical {@code App} classes: holds the
 * {@code start()}/{@code run()}/{@code close()} boilerplate — drain {@code getAdded()}, filter each
 * entry through {@link #accepts}, {@link #unwrap} the container's payload to {@code I},
 * {@code processEvent}, {@code send}, {@code close} — delegating all actual transform logic to the
 * subclass's core. The drain loop and for which {@code StriimOpenProcessor}/{@code Processor} abstract
 * methods a concrete {@code App} had to implement ({@code run}, {@code setOpenProcessor},
 * {@code getAggVec}/{@code setAggVec}). <b>That is history, not a pointer:</b> that class now
 * extends {@link AbstractOpenProcessorApp} and implements none of them.</p>
 *
 * <p>Keeping this shell humble is what lets each OP's core be unit tested without a live Striim
 * runtime: the subclass only implements {@link #buildProcessor}, and every other lifecycle method
 * lives here, once.</p>
 */
public abstract class AbstractConvertingOpenProcessorApp<I extends com.webaction.event.Event>
        extends StriimOpenProcessor {

    /**
     * Reserved props keys — the runtime namespace/source name of the deployed OP,
     * injected here from the framework so a type-creating core can build target
     * type names; NOT a {@code @PropertyTemplate} property.
     */
    public static final String NAMESPACE_KEY = "striim.op.namespace";
    /**
     * Reserved props keys — the runtime namespace/source name of the deployed OP,
     * injected here from the framework so a type-creating core can build target
     * type names; NOT a {@code @PropertyTemplate} property.
     */
    public static final String SOURCE_NAME_KEY = "striim.op.sourceName";
    /**
     * Reserved props keys — the runtime UUID of the deployed OP component,
     * injected here from the framework so an OP can access component-level MDR checkpoints;
     * NOT a {@code @PropertyTemplate} property.
     */
    public static final String COMPONENT_UUID_KEY = "striim.op.componentUuid";

    private OpenProcessor baseOpenProcessor;
    protected ConvertingEventProcessor<I, WAEvent> processor;

    protected Logger logger;

    /*
     * On cross-thread visibility of `logger` and `processor` -- both plain fields, read by whatever
     * background thread an AsyncEmittingProcessor core starts: start() assigns them, injects the
     * emitter, and only THEN calls processor.start(), which is where such a thread is EXPECTED to be
     * created -- a convention this ordering depends on, not something AsyncEmittingProcessor
     * enforces. Thread.start() happens-before every action in the started
     * thread (JLS 17.4.5), so that thread is guaranteed to observe both. No volatile needed -- which
     * matters because `logger` is read on every event in the run() loop.
     *
     * A core that starts its thread in its CONSTRUCTOR forfeits exactly this: the thread then exists
     * before these writes, with no edge at all.
     */

    /**
     * The recovery position of the most recent event this app processed, for {@link #emitAsync}.
     *
     * <p>{@code volatile} because it is written on the framework's batch thread and read on
     * whatever background thread emits — that cross-thread read is the entire reason it exists.</p>
     */
    private volatile ImmutableStemma lastPosition;

    /**
     * Warns at most twice if this OP fans out under recovery — see {@link FanOutRecoveryWarning}.
     * Lives here rather than in each OP because the run() loop below is the emission choke point
     * every OP shares, so one implementation covers them all instead of per-OP copies that drift.
     *
     * <p><b>It covers the run() loop, NOT emitAsync.</b> A background flush that releases many
     * events on one borrowed {@code lastPosition} — a cache-eviction path, say —
     * goes out through {@code emitAsync} and is not observed here. Stated because "single choke
     * point" reads as complete coverage and it is not.</p>
     *
     * <p>Reassigned in start() so the message can name the component; the initializer only exists
     * so a warn arriving before start() completes cannot NPE.</p>
     */
    private FanOutRecoveryWarning fanOutRecoveryWarning = new FanOutRecoveryWarning(null);

    /** The fan-out diagnostic, for a subclass that sends outside {@link #emitGroup}. */
    protected final FanOutRecoveryWarning fanOutRecoveryWarning() {
        return fanOutRecoveryWarning;
    }

    /**
     * Subclass builds its core from the {@code props} passed in — the sole declared
     * constructor's parameters injected by type (any subset/order of {@code Map},
     * {@link BuiltInFuncs}, {@link com.example.common.TypeResolver}, {@link Logger}
     * per the Gold Standard rule), NOT necessarily {@code getProperties()} verbatim:
     * {@link #start} passes an enriched copy carrying the reserved
     * {@link #NAMESPACE_KEY}/{@link #SOURCE_NAME_KEY} keys a type-creating core needs.
     */
    protected abstract ConvertingEventProcessor<I, WAEvent> buildProcessor(Map<String, Object> props)
            throws Exception;

    /**
     * Reports any received TQL property this App never declared.
     *
     * <p>Striim accepts an unknown property in a {@code USING (…)} clause without complaint, so a
     * typo'd or retired name deploys clean and the operator silently runs on the default. Every
     * OP in the upgrade programme gets a version bump and every bump is a TQL edit, which is
     * exactly when that happens.</p>
     *
     * <p><b>Warns; does not refuse.</b> See {@link DeclaredProperties} for why, and for the
     * condition on promoting this to a startup failure.</p>
     */
    private void warnAboutUndeclaredProperties(Map<String, Object> props) {
        DeclaredProperties.report(getClass(), props, logger);
    }

    /**
     * The payload kind this OP consumes, used to unwrap each batch entry.
     *
     * <p><b>A runtime-checked cast, not an erased one.</b> {@code (I) container.data} would compile
     * to a cast to the type BOUND and defer the failure into {@code processEvent}, moving it inside
     * the try where the error model would swallow what used to escape. Casting through the class
     * token keeps the {@code ClassCastException} exactly where it has always been raised — see
     * {@link #accepts} for why that placement is load-bearing.</p>
     *
     * <p>{@link AbstractOpenProcessorApp} answers {@code WAEvent.class} and marks it {@code final},
     * so the eight OPs on that base neither implement nor think about this.</p>
     */
    protected abstract Class<I> inputType();

    /**
     * Unwraps one batch entry's payload to this OP's input kind.
     *
     * <p>Default: a checked cast through {@link #inputType()}, which raises
     * {@code ClassCastException} for a foreign payload — <b>outside the per-event try, uncaught by
     * the error model</b>, exactly where the hand-rolled loops raised it. See {@link #accepts} for
     * why that placement matters and when to filter instead.</p>
     *
     * <p>Override only to enrich that failure — to count it, or to name the offending type and the
     * stream position. An override MUST still fail for a payload it cannot convert: returning
     * {@code null} or a substitute would put a fabricated event into the pipeline.</p>
     */
    protected I unwrap(Object payload) {
        return inputType().cast(payload);
    }

    /**
     * Events a core has buffered and can now release, drained at each BATCH BOUNDARY — once before
     * the batch and once after. Default: none.
     *
     * <p><b>This exists for its ERROR MODEL, not its timing.</b> A core that buffers across events
     * (reassembling a source transaction, say) may also run a background timer that
     * releases on a TTL, and a timer alone looks sufficient: it fires far more often than the TTL
     * is long. That reasoning would delete these calls as redundant, and it is wrong — because it compares only WHEN each path fires, not what
     * happens when the SEND fails.</p>
     *
     * <p>A drain REMOVES the events from the core's buffer before returning them, so they exist
     * only in the returned list. Sent from here they are inside the per-batch try: a send failure
     * fails the batch under {@link #failBatchOnEventError()} and the framework does not checkpoint,
     * so the events replay. Sent from a background thread they go through {@link #emitAsync}, whose
     * failure handler logs and continues — correct for a timer, which has no batch to fail, but it
     * means a failed send DROPS them silently while the batch checkpoints past. Keeping the
     * boundary drain keeps the common path on the recoverable route.</p>
     *
     * <p>Returning an empty list is the no-op, and a core with no buffer inherits exactly that.</p>
     */
    protected java.util.List<WAEvent> drainAtBatchBoundary() {
        return java.util.List.of();
    }

    /**
     * Sends whatever {@link #drainAtBatchBoundary} released, on the batch thread.
     *
     * <p>Borrows {@link #lastPosition} — a boundary drain has no single upstream event in scope, and
     * {@code ImmutableStemma} cannot be constructed, so the most recent completed position is the
     * closest available answer. Same borrowing the old hand-rolled loops did.</p>
     */
    private void sendBoundaryDrain(boolean afterBatch) throws Exception {
        // The synthetic transaction batch first: its release decides the run's fate under every
        // error model (AbstractOpenProcessorApp), and a core's own drain failing must not skip it.
        releaseTransactionBatchAtBoundary(afterBatch);
        PositionPropagation.sendAllWithPosition(this, drainAtBatchBoundary(), lastPosition);
    }

    /**
     * The synthetic transaction batch's own boundary hook, beside {@link #drainAtBatchBoundary}:
     * called once before and once after every platform batch, on the batch thread, inside the
     * same error model. The override SENDS for itself (each event carries its own source
     * position, and a failed send must fail the run — see {@link AbstractOpenProcessorApp}).
     * {@code afterBatch} says which of the two calls this is. Default: nothing.
     */
    protected void releaseTransactionBatchAtBoundary(boolean afterBatch) throws Exception {
    }

    /**
     * Sends everything one source event produced, after {@link #beforeSend}: the fan-out
     * diagnostic, then ONE send carrying {@code stemma} on every member (design §5.2).
     * {@link AbstractOpenProcessorApp} overrides this to hold the group back when a synthetic
     * transaction batch is open.
     */
    protected void emitGroup(I source, java.util.List<WAEvent> outEvents, ImmutableStemma stemma) throws Exception {
        // Before emitting, not after: if a send throws mid-group the operator still needs
        // to know the group existed. At most two lines for the life of the component.
        //
        // Its OWN try/catch, and that is not defensive padding. This call sits inside the
        // region governed by failBatchOnEventError(): an escape from a pure DIAGNOSTIC
        // would either fail the whole batch (converters) or skip the transform and
        // pass the untransformed source event through instead (enrichers/mappers) --
        // silent semantic corruption caused by a log helper. Nothing may be worth that.
        try {
            fanOutRecoveryWarning.observe(outEvents, stemma, logger);
        } catch (RuntimeException diagnosticFailure) {
            // Deliberately swallowed and NOT re-logged through `logger`: the most likely
            // way to arrive here is a logger that is already failing.
        }
        // ONE send for the whole group (design §5.2): all-or-nothing at the
        // emission boundary, so a send failure cannot leave a partial fan-out downstream.
        final int n = outEvents == null ? 0 : outEvents.size();
        logger.log(() -> "Sending " + n + " event(s)");
        PositionPropagation.sendAllWithPosition(this, outEvents, stemma);
    }

    /**
     * The last word on what is sent for one source event: takes what the core produced and
     * returns what the loop emits, in order. Default: the core's list, untouched.
     *
     * <p>Exists so that a base bound to a concrete input kind can FRAME a group without owning
     * the loop — {@link AbstractOpenProcessorApp} overrides it to wrap the group in synthetic
     * transaction markers ({@link SyntheticTransactionBoundaries}). Called inside
     * the per-event try, so a failure here is governed by {@link #failBatchOnEventError()} like a
     * transform failure, and called for the error-model passthrough too, so a forwarded original
     * is framed exactly as a transformed one would have been.</p>
     *
     * <p>The parameter is the whole group rather than one event because a frame is a property of
     * the group: a per-event hook could not put one marker before the first sibling and one after
     * the last.</p>
     */
    protected java.util.List<WAEvent> beforeSend(I source, java.util.List<WAEvent> emitted) {
        return emitted;
    }

    /**
     * Called once for every event this loop fails on, BEFORE the error model acts — before the
     * fail-batch rethrow and before {@link #passThroughOnError}. Default: no-op.
     *
     * <p>Exists so a core can keep its own failure COUNTER accurate. The loop is the only place
     * that sees the whole failure surface: a core's {@code processEvent} can count what it throws
     * itself, but not an exception it rethrows for the caller to classify, and not a {@code send}
     * failure, which happens after it has returned. A hand-rolled {@code App} counted both in its
     * own catch; without this hook that counting is silently lost when it moves onto this shell,
     * and a dashboard reading the counter reports fewer errors than occurred.</p>
     *
     * <p><b>Must not throw.</b> It runs inside the catch, so an exception from here would replace
     * the original failure and lose it.</p>
     */
    protected void onEventError(Exception e) {
    }

    /**
     * Error model for this OP: choose whether an event failure fails the batch.
     * Default {@code false}: a core transform
     * or send failure is logged and the original event is best-effort forwarded
     * (enricher/mapper) — the behavior every OP built on this shell had before this
     * hook existed, so a non-overriding subclass sees no change. A writer/sink or CDC
     * format converter overrides this to {@code true}, so the failure propagates out
     * of {@link #run} instead and the framework does NOT checkpoint past the failed
     * event. A fail-batch override does NOT attempt the best-effort passthrough send
     * before propagating — sending an event whose side effect failed is precisely the
     * data-integrity hazard this model exists to prevent; do not "fix" that. This flag
     * also governs a batch-fetch ({@code getAdded()}) failure, not just a per-event
     * one — both are checkpoint-relevant failures under the same error model.
     */
    protected boolean failBatchOnEventError() {
        return false;
    }

    /** Exception-sensitive policy; existing subclasses retain their no-argument override. */
    protected boolean failBatchOnEventError(Exception failure) {
        return failBatchOnEventError();
    }

    /**
     * Batch-entry filter, consulted with the raw payload of each entry —
     * {@code container.data}, before it is cast. Default {@code true}: every entry is
     * unwrapped and processed, which is the behavior every OP built on this shell had
     * before this hook existed, so a non-overriding subclass sees no change.
     *
     * <p>{@link #unwrap}'s cast of that payload to {@code I} — {@code proc.events.WAEvent} for an
     * OP on {@link AbstractOpenProcessorApp} — is not {@code instanceof}-checked and sits
     * <em>outside</em> the try, so an entry carrying any other payload raises a
     * {@code ClassCastException} that no error model catches,
     * {@link #failBatchOnEventError()} included. An OP that knows a foreign payload can
     * reach its stream, and that skipping it is the correct response, overrides this to
     * return {@code false} for it. Log the skip in the override, not here: only the
     * subclass knows whether it is routine or remarkable.
     *
     * <p>The parameter is the payload rather than the enclosing
     * {@code runtime.containers.WAEvent} deliberately: a filter decides on what the event
     * IS, and taking {@code Object} keeps the platform container off every implementer's
     * signature — so an override is testable without constructing a platform type, and
     * naming one does not become the price of adopting this hook.
     *
     * <p><b>An override must not throw.</b> This is consulted before the per-event try, so an
     * exception out of it escapes {@code run()} unlogged and unclassified — bypassing both the
     * failure logging and {@link #failBatchOnEventError()}. A filter decides; it does not fail.
     * Anything that can fail belongs in {@code processEvent}, where the error model applies.
     *
     * <p>Returning {@code false} is a <b>filter, not a failure</b> — the entry is
     * skipped, the rest of the batch still runs, and the checkpoint advances past it.
     * An entry that must NOT be checkpointed past is an error, so express that by
     * throwing from {@code processEvent} under {@code failBatchOnEventError() == true}
     * instead.
     */
    protected boolean accepts(Object payload) {
        return true;
    }

    @Override
    public void start() throws Exception {
        super.start();

        Objects.requireNonNull(baseOpenProcessor,
                "setOpenProcessor() must be called before start() — Striim framework contract violated");

        Map<String, Object> props = getProperties();
        String namespace = baseOpenProcessor.getMetaNsName();
        String sourceName = baseOpenProcessor.getMetaName();
        boolean enableLogging = Boolean.parseBoolean(Objects.toString(props.get("EnableLogging"), "false"));
        logger = new Logger(namespace + "." + sourceName, enableLogging);
        warnAboutUndeclaredProperties(props);
        fanOutRecoveryWarning = new FanOutRecoveryWarning(namespace + "." + sourceName);

        // Enrich a mutable copy with the reserved namespace/sourceName keys (not
        // @PropertyTemplate properties) so a type-creating core can build target type
        // names. Reuses the same two values the logger tag above was built from, so the
        // tag and the injected keys can never disagree.
        Map<String, Object> enrichedProps = new HashMap<>(props);
        if (namespace != null) {
            enrichedProps.put(NAMESPACE_KEY, namespace);
        }
        if (sourceName != null) {
            enrichedProps.put(SOURCE_NAME_KEY, sourceName);
        }
        if (baseOpenProcessor != null && baseOpenProcessor.getMetaID() != null) {
            enrichedProps.put(COMPONENT_UUID_KEY, baseOpenProcessor.getMetaID());
        }

        processor = buildProcessor(enrichedProps);

        // Opt-in: only a core that emits outside processEvent implements this. Injected
        // BEFORE start() so a background thread the core starts there never races a null
        // emitter. See AsyncEmittingProcessor.
        if (processor instanceof AsyncEmittingProcessor) {
            ((AsyncEmittingProcessor) processor).setAsyncEmitter(new ShellAsyncEmitter(this));
        }

        processor.start();
    }

    @Override
    public void run() {
        IBatch<com.webaction.runtime.containers.WAEvent> batch;
        try {
            batch = getAdded();
        } catch (Exception e) {
            logFailure("Exception fetching batch in run(): ", e);
            if (failBatchOnEventError(e)) {
                throw asRuntime("Exception fetching batch in run(): ", e);
            }
            return;
        }
        if (batch == null) {
            return;
        }
        // Pre-batch drain, before any of this batch's events. Inside the error model: see
        // drainAtBatchBoundary() for why a background timer is not a substitute for this call.
        try {
            sendBoundaryDrain(false);
        } catch (Exception e) {
            onEventError(e);
            logFailure("Exception draining buffered events before batch in run(): ", e);
            if (failBatchOnEventError(e)) {
                throw asRuntime("Exception draining buffered events before batch in run(): ", e);
            }
        }
        for (com.webaction.runtime.containers.WAEvent container : batch) {
            // Filter before the unwrap: the cast below is not instanceof-checked, so an
            // entry a subclass declines is skipped here or not at all (see accepts()).
            if (!accepts(container.data)) {
                continue;
            }
            I source = unwrap(container.data);
            // Carry the source's own recovery/checkpoint position onto every event
            // emitted for it — including fan-out — instead of send(Object)'s
            // batch-wide merged position (see PositionPropagation). May be null itself
            // (e.g. no upstream checkpointing); that's carried through as-is.
            ImmutableStemma stemma = container.position;
            // Both processEvent() and send() are guarded together: a core's transform
            // failure and a framework send failure are handled identically — log, then
            // either best-effort pass the original event through (enricher/mapper
            // error model, which every subclass now inherits from here) or propagate out of
            // run() so the framework does not checkpoint past the failed event
            // (writer/sink and CDC-converter error model), per failBatchOnEventError().
            try {
                emitGroup(source, beforeSend(source, processor.processEvent(source)), stemma);
            } catch (Exception e) {
                onEventError(e);
                logFailure("Exception processing/sending in run(): ", e);
                if (failBatchOnEventError(e)) {
                    throw asRuntime("Exception processing/sending in run(): ", e);
                }
                try {
                    passThroughOnError(source, stemma);
                } catch (Exception sendError) {
                    logger.logError(() -> "Failed to pass through event after error: " + sendError.toString());
                    if (failBatchOnEventError(sendError)) {
                        throw asRuntime("Failed to pass through event after error: ", sendError);
                    }
                }
            }

            // AFTER the event has been emitted, never before. An async emit borrows this position;
            // publishing event N's position while N is still in flight would claim progress that has
            // not happened, and a crash there loses N. Trailing by one event replays, never skips.
            //
            // Deliberately NOT in a finally. Under failBatchOnEventError() the catch above rethrows,
            // and a finally would advance past the very event whose batch is being failed -- handing
            // an async emitter a position that checkpoints past it and defeating the one guarantee
            // that error model exists to provide. Falling through instead skips the advance on that
            // path.
            //
            // Caveat, inherited rather than introduced: under the default model the catch passes the
            // original event through, so reaching here normally does mean it was emitted -- unless
            // that passthrough send ALSO failed, which is logged and swallowed. Then this advances
            // over an event nothing was emitted for. Pre-existing on every hand-rolled run(); fixing
            // it needs a low-watermark, not a placement tweak.
            lastPosition = stemma;
        }

        // Post-batch drain: this batch's events may have completed a buffered group, and the
        // pre-batch call above cannot have seen them.
        try {
            sendBoundaryDrain(true);
        } catch (Exception e) {
            onEventError(e);
            logFailure("Exception draining buffered events after batch in run(): ", e);
            if (failBatchOnEventError(e)) {
                throw asRuntime("Exception draining buffered events after batch in run(): ", e);
            }
        }
        afterBatch();
    }

    /**
     * Called once per batch, after every event and the post-batch drain have been SENT — the
     * last thing {@link #run} does on the batch thread before it returns and the batch is acked.
     *
     * <p>The place for a core to make durable whatever this batch changed in state the platform
     * checkpoint does not see (design §2.3: a buffering OP contributes nothing to it). Sitting
     * after the sends and before the ack gives emit-then-persist ordering: a crash between the two
     * can re-emit, never lose. Not reached when the batch failed under
     * {@link #failBatchOnEventError()}: whatever that batch changed in memory stays there and is
     * written by the next call that gets here, or by {@code close()}; the batch itself replays,
     * and recognising its events on replay is the core's job. Default: nothing.</p>
     */
    protected void afterBatch() {
    }

    /**
     * The default error model's best-effort passthrough: forward the ORIGINAL event when the core
     * failed on it.
     *
     * <p><b>Meaningful only when the input kind is what the platform can send</b>, which is why it
     * is a hook rather than a {@code send} call inline. A same-kind OP forwards the event —
     * {@link AbstractOpenProcessorApp} overrides this to do exactly that, so the eight OPs on that
     * base are unaffected. A CONVERTER cannot: its input is an {@code AvroEvent} or similar, and
     * the send path takes {@code proc.events.WAEvent}. There is no sensible passthrough for a
     * converted kind, so the default here does nothing but say so.</p>
     *
     * <p>A converter should therefore override {@link #failBatchOnEventError()} to {@code true} —
     * which every CDC format converter already must, since a lost change cannot be checkpointed
     * past. Reaching this default at all means a converter is running the passthrough model, and
     * the log line is the warning that its failures are being dropped rather than forwarded.</p>
     */
    protected void passThroughOnError(I source, ImmutableStemma stemma) throws Exception {
        logger.logWarn(() -> "No passthrough for a converted event kind ("
                + (source == null ? "null" : source.getClass().getName())
                + "); the event was dropped. A converting OP should override failBatchOnEventError()"
                + " to true so the batch fails instead.");
    }

    /**
     * Emits an event from <b>outside</b> the batch loop — a retry sweep, a timed flush, any
     * background drain — carrying the position of the last event the app processed.
     *
     * <p><b>Why this belongs here rather than in each op.</b> An op that emits asynchronously needs
     * two things the batch loop owns privately: the framework handle to send through, and a
     * recovery position to attach. Hand-rolled, that means every such op keeps its own
     * {@code volatile} position field updated in its own copy of the run loop — which is how a
     * module ends up re-implementing the shell it was supposed to inherit. Here the base owns both
     * halves and the subclass writes {@code emitAsync(event)}.</p>
     *
     * <p><b>The position is the last one this app finished emitting, and that is an approximation
     * with a known hole.</b> An event emitted later has no position of its own —
     * {@code ImmutableStemma} cannot be constructed from scratch, only carried — so the closest
     * available answer is the most recent completed one.</p>
     *
     * <p><b>This does NOT guarantee recovery resumes at or before the async event's true origin.</b>
     * That origin is the earlier event which queued the work; the position attached here is the
     * latest completed one, which is typically <i>past</i> it. A crash between queuing and emitting
     * can still lose the queued work on replay. This is a genuine data-loss window, pre-existing and
     * caused by holding work past the completed input position. No borrowed position closes
     * it; a core that must hold work
     * releases it on {@code flush()} and persists it through {@code ComponentStateCheckpointer}
     * (§9.5–§9.6). What the placement DOES guarantee is narrower and still worth having: the
     * position never runs ahead of what this app has actually emitted.</p>
     *
     * <p>Emitting before any batch has been processed carries a {@code null} position, which
     * {@link PositionPropagation} already treats as "no upstream checkpointing" — the same as an
     * unpositioned source event.</p>
     */
    protected final void emitAsync(WAEvent event) throws Exception {
        PositionPropagation.sendWithPosition(this, event, lastPosition);
    }

    /**
     * The {@link AsyncEmitter} handed to an {@link AsyncEmittingProcessor}: {@link #emitAsync} with
     * the one correct failure handler, written once here instead of once per op.
     *
     * <p>Logs and returns rather than propagating, so a core emitting a run of events does not lose
     * the rest of the run to one failure — the continue-on-failure behaviour the hand-wired version
     * of this pattern had.</p>
     */
    void emitAsyncLogged(WAEvent event) {
        try {
            emitAsync(event);
        } catch (Exception e) {
            logger.logError(() -> "Async emit failed: " + e);
        }
    }

    /**
     * {@link #emitAsyncLogged} for a GROUP: one {@code TaskEvent} carrying every member at
     * {@code lastPosition}, so a background release is all-or-nothing at the emission boundary
     * (design §5.2). Same failure contract: logged, never thrown.
     */
    void emitAllAsyncLogged(java.util.List<? extends WAEvent> events) {
        try {
            PositionPropagation.sendAllWithPosition(this, events, lastPosition);
        } catch (Exception e) {
            final int n = events == null ? 0 : events.size();
            logger.logError(() -> "Async group emit failed (" + n + " event(s)): " + e);
        }
    }

    /**
     * Fail-batch propagation that <b>preserves the throwable's identity</b>.
     *
     * <p>This is not a style choice — the platform reads the exception's class name and acts on
     * it. Verified against the 5.4.0.6 jars: {@code ExceptionType.getExceptionType} classifies on
     * {@code throwable.getClass().getSimpleName()} (it never unwraps a cause — the unwrapping,
     * and only for {@code StriimException}/{@code StriimRuntimeException}, happens one level up
     * in {@code FlowComponent.notifyAppMgr}), and {@code NodeManager}'s default
     * handler looks that same simple name up in a map, falling through to {@code CRASH} when it
     * is absent. Wrapping everything in a plain {@code RuntimeException} therefore collapses
     * every distinct failure to one name that is in neither that map nor any application's
     * {@code EXCEPTIONHANDLER} clause — turning the platform's {@code HALT} actions
     * ({@code ArithmeticException}, {@code NumberFormatException}, {@code ConnectionException},
     * {@code AdapterException}, …) into {@code CRASH} with no customer configuration involved,
     * and stopping {@code EXCEPTIONHANDLER (NullPointerException: IGNORE)} from ever matching.
     *
     * <p>So an unchecked throwable is rethrown <b>as itself</b>. Only a checked one is wrapped,
     * because {@code run()} cannot declare {@code throws} — and that branch is defensive rather
     * than load-bearing: no method this loop calls declares a checked exception that can reach
     * it on 5.4.x ({@code OpenProcessor.doOutput} catches and logs {@code Exception} internally).
     * The context string the wrapper used to add is still on the log line above every call site,
     * so nothing is lost from the operator's own output. One surface does change: the platform
     * copies {@code e.getMessage()} into the {@code ExceptionEvent} it stores, so the monitoring
     * view now shows the original's message (which for an NPE is often {@code null}) rather than
     * "Exception processing/sending in run(): ...". Correct trade — an accurate CLASS is worth
     * more than a decorated message, and the class is what decides HALT vs CRASH — but it is a
     * trade, not a free win.
     *
     * <p>Rejected alternative: wrapping in {@code StriimRuntimeException}, which the platform
     * <i>does</i> unwrap. It would put a platform type into the class whose purpose is keeping
     * platform types out of a core, and {@code NodeManager} consults its
     * {@code getExpectedAppStatus()} <b>before</b> the type map — so a wrapper built with the
     * wrong status silently overrides the application's own error policy.
     */
    private static RuntimeException asRuntime(String context, Exception e) {
        if (e instanceof RuntimeException) {
            return (RuntimeException) e;
        }
        return new RuntimeException(context + e, e);
    }

    /**
     * ERROR for a failure, WARN for a shutdown. A stopping app interrupts its threads, and the
     * interruption surfaces here as an exception out of {@code send()} — which is not a defect
     * and should not be logged as one, since every graceful stop would then report an error it
     * did not have. Matching on {@code InterruptedException} in the cause chain rather than on
     * the platform's {@code RuntimeInterruptedException} keeps this free of platform imports,
     * and is also more accurate: that class is constructible from a kryonet
     * {@code TimeoutException} too, which IS a real failure and stays at ERROR.
     */
    private void logFailure(String context, Exception e) {
        if (isInterrupt(e)) {
            logger.logWarn(() -> context + e);
        } else {
            logger.logError(() -> context + e);
        }
    }

    private static boolean isInterrupt(Throwable t) {
        // The CAUSE CHAIN only, deliberately. An earlier revision also fell back to
        // Thread.currentThread().isInterrupted(), which is not cleared by reading it: any
        // library that catches InterruptedException and politely restores the flag leaves it
        // set for the life of that thread, after which EVERY genuine failure -- NPE, connection
        // error, anything -- would log at WARN instead of ERROR, permanently and undetectably.
        // That is worst in the passthrough error model, where the log line is the only evidence
        // a failure happened at all. The platform's own interrupt detection
        // (FlowComponent.notifyAppMgr) walks the chain with no such fallback either.
        //
        // Bounded walk: a self-referential or cyclic cause chain is malformed but must not hang
        // the drain loop.
        Throwable c = t;
        for (int depth = 0; c != null && depth < 16; depth++) {
            if (c instanceof InterruptedException) {
                return true;
            }
            c = (c.getCause() == c) ? null : c.getCause();
        }
        return false;
    }

    @Override
    public void setOpenProcessor(OpenProcessor openProcessor) {
        this.baseOpenProcessor = openProcessor;
        super.setOpenProcessor(openProcessor);
    }

    /** For subclasses that need namespace/sourceName (e.g. type-consuming OPs). */
    protected OpenProcessor getBaseOpenProcessor() {
        return baseOpenProcessor;
    }

    /**
     * This OP's owning application as {@code namespace.appName}, or {@code null} while the
     * platform has not attached the flow yet.
     *
     * <p><b>The same contract as {@link AbstractReaderApp#appQualifiedName()}, reached
     * differently.</b> {@code StriimOpenProcessor} is not a {@code BaseProcess}, so there is no
     * inherited {@code getOwnerFlow()} to read — but the {@code OpenProcessor} the framework hands
     * this class <i>is</i> a {@code FlowComponent} ({@code OpenProcessor extends FlowComponent}),
     * so the top-level flow is one hop away.
     * Java's single inheritance is what forces two reaches. Both walk from the component
     * ({@code getTopLevelFlow()}) and both end at the same {@link FlowIdentity} decision, so the
     * two agree on what a qualified name means AND on when it is available.</p>
     *
     * <p><b>Keeping those two expressions the same is deliberate.</b> {@link AbstractReaderApp}
     * could have read {@code BaseProcess.getOwnerFlow()} instead — shorter, and set to the
     * top-level flow before {@code init()} on the {@code Source} path. It does not, partly because
     * that field is never populated on the cache-adapter path, and partly because it is a
     * <i>snapshot</i> where this is a live read: the two would then diverge for any component whose
     * flow is detached after start. One pattern means one expression.</p>
     *
     * <p>{@code null} is a defer signal, never a value to substitute for — see
     * {@link FlowIdentity#compose(String, String)}.</p>
     */
    public String appQualifiedName() {
        try {
            OpenProcessor component = baseOpenProcessor;
            if (component == null) {
                return null;
            }
            Flow app = component.getTopLevelFlow();
            if (app == null) {
                return null;
            }
            MetaInfo.Flow info = app.flowInfo;
            return (info == null) ? null : FlowIdentity.compose(info.getNsName(), info.getName());
        } catch (Throwable ignore) {
            // Naming must degrade to "not yet", never take down the OP.
            return null;
        }
    }

    /**
     * This OP component as {@code namespace.opName}, or {@code null} before
     * {@code setOpenProcessor()}.
     *
     * <p>Composed from the same two values {@link #start()} already reads for
     * {@link #NAMESPACE_KEY}/{@link #SOURCE_NAME_KEY}, so the injected property keys, the logger
     * tag and this name can never disagree.</p>
     *
     * <p>Prefer this over {@link #appQualifiedName()} when keying per-OP state: it is strictly
     * more specific — it distinguishes two instances of the same OP inside one application — and
     * it does not depend on the flow being wired.</p>
     */
    public String componentQualifiedName() {
        try {
            OpenProcessor component = baseOpenProcessor;
            if (component == null) {
                return null;
            }
            return FlowIdentity.compose(component.getMetaNsName(), component.getMetaName());
        } catch (Throwable ignore) {
            return null;
        }
    }

    @Override
    public void flush() throws Exception {
        if (processor instanceof java.io.Flushable) {
            ((java.io.Flushable) processor).flush();
        }
        super.flush();
    }

    @Override
    public void close() throws Exception {
        if (processor != null) {
            processor.close();
        }
        super.close();
        // logger is null if start() failed before it was constructed; guard so a
        // failed-start teardown does not NPE.
        if (logger != null) {
            logger.log(() -> "App closed");
        }
    }

    @Override
    public Map getAggVec() {
        return null;
    }

    @Override
    public void setAggVec(Map aggVec) {
        // No-op
    }
}
