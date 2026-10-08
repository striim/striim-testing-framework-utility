package com.example.common;

import com.webaction.event.Event;
import com.webaction.proc.RetriableWriter;
import com.webaction.recovery.Acknowledgeable;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.Position;
import com.webaction.runtime.monitor.MonitorEvent;
import com.webaction.runtime.monitor.MonitorEventsCollection;
import com.webaction.source.lib.prop.Property;
import com.webaction.source.lib.prop.RetryPolicy;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Timer;
import java.util.TimerTask;

/**
 * Base for a field-built Striim writer target.
 *
 * <p>Extends {@code RetriableWriter} — the same base the built-in {@code DatabaseWriter} uses —
 * and implements {@code Acknowledgeable}, which is <b>not optional</b>. Both points are verified
 * rather than assumed; the recovery design and database-writer status documents both record the
 * evidence.</p>
 *
 * <h2>Why {@code Acknowledgeable} is mandatory</h2>
 * It is an <b>empty marker interface</b>, and {@code Target} keys its entire delivery model on it:
 * <pre>{@code
 * if (!this.useDeliveryCallback && this.adapter instanceof Acknowledgeable) {
 *     this.useDeliveryCallback = true;
 * }
 * }</pre>
 * That flag gates both {@code Target.memoryCheckpoint} — which pins positions received and not yet
 * acked, holding the app checkpoint back — and the injection of {@code receiptCallback}. Measured
 * A/B: an identical writer without the marker never receives a callback and is never pinned, so a
 * batching writer silently loses whatever it had not committed when the checkpoint advanced.
 * Nothing warns you. There is no compiler help, because the interface declares no methods.
 *
 * <h2>The ordering that makes recovery correct</h2>
 * Events arrive one at a time; this class accumulates them and flushes through {@link WriteCycle},
 * which applies-and-commits <b>before</b> acking. Positions stay pinned until their data is
 * durable, so the app checkpoint can never move past the target's state.
 *
 * <p>Note the endpoint checkpoint does <b>not</b> rewind replay — it is a duplicate filter over
 * whatever the source resends, as the recovery design document sets out. It cannot rescue a writer
 * that acked too early.</p>
 *
 * <h2>Error model — fail the batch</h2>
 * This is a <b>writer/sink</b>, so
 * it takes the writer role: <b>a failed apply rethrows out of {@link #processEvent}</b> rather than
 * being absorbed. Nothing is acked, the positions stay pinned, and the framework cannot checkpoint
 * past the failed event. The alternative — best-effort passthrough — is right for an enricher and
 * catastrophic here: a silently dropped write is data loss the source position would skip forever.
 *
 * <p>{@code failBatchOnEventError()} and {@code accepts(Object)} are
 * {@code AbstractOpenProcessorApp} seams and do not apply; a Target reaches the same outcome by
 * letting the exception leave {@code processEvent}.</p>
 *
 * <p>Subclasses implement {@link #applyBatch} (apply and commit) and {@link #durablePosition}
 * (what has been committed). Everything else here is the wiring those two need.</p>
 */
public abstract class AbstractWriterApp extends RetriableWriter implements Acknowledgeable {

    /**
     * Retry wait used only when the platform hands us no property object at all.
     *
     * <p><b>Milliseconds, and that is the trap.</b> {@code RetryPolicy}'s constructor takes
     * {@code (wait, count)} and {@code Property.parseRetryPolicy} multiplies a bare
     * {@code retryInterval=30} by 1000 before storing it, so the platform's own default is 30_000
     * here, not 30. Passing 30 compiles, reads correctly, and retries a downed target a thousand
     * times faster than configured.</p>
     */
    private static final int DEFAULT_RETRY_WAIT_MILLIS = 30_000;

    /** Retry count used only when the platform hands us no property object at all. */
    private static final int DEFAULT_MAX_RETRIES = 3;

    /**
     * The shared field {@link Logger}, not log4j and not {@code java.util.logging}.
     *
     * <p>It is the one log seam, and its backend is expected to move to log4j — routing
     * through it means that change reaches this writer for free, while a direct log4j call here
     * would have to be found and rewritten.</p>
     *
     * <p>Created in {@link #init} rather than injected, because the platform constructs an adapter
     * through a no-arg constructor — there is no seam to inject through. {@link #setLogger} exists
     * so a subclass or test can still substitute one.</p>
     */
    private Logger logger = new Logger("DatabaseWriter", false);

    /** Mirrors the built-in's {@code BatchPolicy} EventCount default. */
    public static final int DEFAULT_BATCH_EVENTS = 1000;
    /** Mirrors the built-in's {@code BatchPolicy} Interval default, in milliseconds. */
    public static final long DEFAULT_BATCH_INTERVAL_MS = 60_000L;

    private BatchAccumulator<WriteItem> accumulator;
    private Timer flushTimer;

    /**
     * The failure from an age-deadline flush, held until a platform-owned thread can be told.
     *
     * <p>Volatile because the timer thread writes it and the receive thread reads it. They do share
     * {@code synchObject}, but {@link #flush()} is reachable without that monitor.</p>
     */
    private volatile Throwable flushFailure;
    /** When {@link #flushFailure} was first seen, so the report can say how long it has stood. */
    private volatile long flushFailedAtMillis;
    /** Timer-thread only: how many times the outstanding failure has been reported, and when next. */
    private long reportsMade;
    private long nextReportAtSeconds;
    /** Escalated to the platform once; the app halts on the first, and later ones add nothing. */
    private volatile boolean escalated;
    /** The escalation ITSELF failing is reported once, so a broken notify cannot flood the log. */
    private volatile boolean escalationFailureReported;
    /** Total events durably applied, for {@code PROCESSED} — the count the monitor page shows. */
    private final java.util.concurrent.atomic.AtomicLong processed =
            new java.util.concurrent.atomic.AtomicLong();
    /**
     * The last commit's numbers, published as ONE immutable object behind ONE volatile reference.
     *
     * <p>As four independent volatile fields, a read from the platform's monitoring thread could
     * interleave with a commit and pair the count from one with the timestamp of another. Nothing
     * is corrupted by that, but a monitor page is read by people making decisions, and a tuple
     * that never existed is worse than a stale one. Null until the first commit — which is also
     * why there is no {@code lastCommitMillis > 0} test: the clock is injectable and zero is a
     * legitimate reading.</p>
     */
    private volatile CommitStats lastCommit;

    /** Guards {@link #cleanup()} against a second {@link #close()}. */
    private boolean cleanedUp;

    @Override
    public void init(final Map<String, Object> properties,
                     final Map<String, Object> formatterProperties,
                     final com.webaction.uuid.UUID inputStream,
                     final String distributionID) throws Exception {
        super.init(properties, formatterProperties, inputStream, distributionID);
        this.logger = new Logger(loggerTag(), debugLoggingEnabled(properties));
        // The undeclared-property guard, matching the in-stream shell. Reporting
        // only; the formatter map is the platform's (handler, formatterName) and is not checked.
        DeclaredProperties.report(getClass(), properties, this.logger);
        this.accumulator = new BatchAccumulator<>(batchEventCount(), batchIntervalMillis());
        // A reconnect calls init() again (RetriableWriter.tryConnect -> cleanup + init); the
        // timer from the first init must not keep ticking beside the new one.
        if (flushTimer != null) {
            flushTimer.cancel();
            flushTimer = null;
        }
        startFlushTimer();
    }

    /**
     * Schedules the age-deadline flush.
     *
     * <p>Without this the deadline never fires. {@link #processEvent} only re-checks on arrival, so
     * a stream that goes quiet mid-batch would leave those positions pinned indefinitely and stall
     * the app checkpoint — the exact stall {@link BatchAccumulator}'s deadline exists to prevent.
     * The built-in does the same thing: {@code BatchPolicy} schedules a {@code java.util.Timer} at
     * the {@code Interval} it was configured with.</p>
     *
     * <p>The task locks {@code synchObject} — the same monitor {@code RetriableWriter.receive} holds
     * while calling {@code processEvent}. Without it the timer could drain the accumulator while an
     * arriving event is being added to it.</p>
     */
    private void startFlushTimer() {
        final long interval = batchIntervalMillis();
        if (interval <= 0) {
            return;
        }
        flushTimer = new Timer(getClass().getSimpleName() + "-flush", true);
        flushTimer.scheduleAtFixedRate(new TimerTask() {
            @Override
            public void run() {
                runFlushTick();
            }
        }, interval, interval);
    }

    /**
     * One tick of the age-deadline timer. <b>Never throws</b> — a timer thread that dies takes every
     * later deadline with it.
     *
     * <p>Package-private and extracted from the {@code TimerTask} on purpose: it is the body the
     * timer actually runs, and a test can drive it directly. The alternative — starting a real timer
     * and sleeping past its interval — is how this repo already acquired a standing CI flake, and a
     * failure path is exactly the wrong place to add a second one.</p>
     */
    void runFlushTick() {
        try {
            reportOutstandingFlushFailure();
            synchronized (synchObject) {
                if (isFlushDue()) {
                    // Read BEFORE flushNow, which drains the accumulator before applying. Reading it
                    // in the catch below reports 0 on every failure, which is what the original
                    // handler did -- it logged "batch of 0 events stays pinned" on every real one.
                    final int draining = pendingCount();
                    try {
                        // An age deadline with events STILL ARRIVING cuts only the completed
                        // prefix (an open transaction goes on accumulating); one that fires
                        // after a full Interval of silence flushes everything -- the reader
                        // has finished delivering, and a run nothing follows is complete.
                        if (accumulator.idleMillis(now()) >= accumulator.maxAgeMillis()) {
                            warnIfHeld("the stream has been quiet for a whole Interval");
                            flushNow();
                        } else {
                            flushCompleted();
                        }
                    } catch (final Throwable t) {
                        recordFlushFailure(t);
                        // A hold() that refuses the window throws BEFORE anything is drained:
                        // the events are still here, unapplied and unacked. Report that, not
                        // "drained and not applied".
                        onFlushTimerError(t, draining - pendingCount());   // 0 when nothing drained
                    }
                }
            }
        } catch (final Throwable t) {
            // isFlushDue/pendingCount themselves failing, or a null monitor. Never let it kill the
            // timer thread, and never let it vanish either.
            recordFlushFailure(t);
            onFlushTimerError(t, -1);
        }
    }

    /**
     * Called when the age-deadline flush throws. <b>Logs at WARN by default — never silently.</b>
     *
     * <p>An earlier version of this method had an empty body, which is the same defect this project
     * documented in the SQL Server driver: it rejects bulk copy and reports it at {@code FINE}, so
     * a user who enabled the feature cannot discover it did nothing.</p>
     *
     * <p>This is a report, <b>not</b> the failure handling. {@link #recordFlushFailure} has already
     * armed the writer to rethrow on the next platform call, so overriding this method cannot
     * silence the failure — only make it less legible. Override to route somewhere better.</p>
     *
     * @param t         what the flush threw
     * @param abandoned how many events had been accumulated, or -1 when the failure happened before
     *                  the batch could be sized
     */
    protected void onFlushTimerError(final Throwable t, final int abandoned) {
        logger.logWarn(() -> "age-deadline flush failed; " + (abandoned == 0
                ? "the window was refused before anything was drained, so its events stay "
                  + "accumulated"
                : (abandoned < 0 ? "the" : abandoned) + " accumulated event(s) were drained and NOT "
                  + "applied") + ". They were never acked, so they stay pinned and replay from the "
                + "source position on restart. This writer is now failed and will rethrow on its "
                + "next event: " + detail(t));
    }

    /**
     * Renders a failure with its stack and its cause chain.
     *
     * <p><b>Why not just {@code t}.</b> A live Oracle run reported
     * {@code NumberFormatException: For input string: "9."} on every apply, repeated for four
     * minutes. That message names no table, no column and no row, and without a stack there is
     * nothing to search for — the log said only that something, somewhere, failed to parse a
     * number. A failure an operator cannot locate is barely better than a silent one.</p>
     *
     * @param t the failure
     * @return its type, message and stack, plus every cause beneath it
     */
    private static String detail(final Throwable t) {
        if (t == null) {
            return "(no exception)";
        }
        final java.io.StringWriter out = new java.io.StringWriter();
        try (java.io.PrintWriter pw = new java.io.PrintWriter(out)) {
            t.printStackTrace(pw);
        }
        return out.toString();
    }

    /**
     * Arms the writer to rethrow a timer-thread failure from the next platform-owned call.
     *
     * <p><b>Why this exists.</b> The count-triggered path lets an apply failure leave
     * {@link #processEvent} and fail the component. The age-deadline path cannot: it runs on a
     * {@code java.util.Timer} thread the platform does not own, so an exception there reaches
     * nothing. Absorbing it left the application RUNNING with a stalled checkpoint and a WARN line
     * — precisely the "reads as a working writer" outcome the fail-the-batch error model exists to
     * prevent, and the common case rather than an edge, since any stream delivering fewer than
     * {@code EventCount} events per {@code Interval} takes this path every time.</p>
     *
     * <p>The first failure is kept. A later one cannot be more informative than the one that
     * started the cascade.</p>
     */
    private void recordFlushFailure(final Throwable t) {
        if (flushFailure == null) {
            flushFailure = t;
            flushFailedAtMillis = now();
        }
        escalateFlushFailure(t);
    }

    /**
     * Tells the platform this component has failed, so the application stops rather than reporting
     * RUNNING over a writer that will never apply anything again.
     *
     * <p><b>Why a report and not a throw.</b> This runs on a {@code java.util.Timer} thread the
     * platform does not own, so an exception here reaches nothing and ends the timer besides.
     * {@code notifyException} is the seam {@code BaseProcess} provides for exactly this: it routes
     * to {@code FlowComponent.notifyAppMgr}, which is what transitions the application.</p>
     *
     * <p><b>Measured, not assumed:</b> before this, a live Oracle run sat at RUNNING for 240
     * seconds with the writer dead and its positions pinned, logging an ERROR every few seconds
     * that nothing acted on. {@link #failIfFlushFailed()} rethrows only from {@code processEvent}
     * and {@code flush()}, and a source that has finished its initial load never calls either
     * again, so the window is unbounded rather than "usually seconds".</p>
     *
     * <p>Escalating cannot be allowed to kill the timer either, so a platform that refuses the
     * notification is logged and the repeating report stays as the fallback.</p>
     *
     * @param t what the flush threw
     */
    private void escalateFlushFailure(final Throwable t) {
        if (escalated) {
            return;
        }
        if (getFlowComponent() == null) {
            // Not attached to a running flow, so there is no application to halt -- a unit test, or
            // a component built but never deployed. Silence here rather than a warning about a
            // platform that is simply absent, and DO NOT latch: nothing was told, so a later tick
            // that finds the component attached must still be able to tell it.
            return;
        }
        try {
            notifyException(t instanceof Exception ? (Exception) t
                    : new IllegalStateException("age-deadline flush failed", t));
            // Latched only once the platform has actually been told. Setting it earlier burned the
            // one shot against a notification that never happened, which would leave the
            // application RUNNING over a dead writer -- the exact failure this method exists for.
            escalated = true;
        } catch (final Throwable escalationFailed) {
            if (!escalationFailureReported) {
                escalationFailureReported = true;
                logger.logWarn(() -> "could not tell the platform this writer has failed, so the "
                        + "application may keep reporting RUNNING; the failure itself stands and is "
                        + "reported on every tick: " + escalationFailed);
            }
        }
    }

    /**
     * Says, on every tick, that an earlier flush failed and nothing has been applied since.
     *
     * <p>{@link #failIfFlushFailed()} rethrows the failure — but only from {@code processEvent} and
     * {@link #flush()}, both of which the platform drives. <b>A writer that stops receiving events
     * is never called again</b>, so the failure is recorded, logged once, and then silent: the
     * component reports RUNNING while its positions stay pinned and the app checkpoint cannot
     * advance past them. One writer surfaces this on the next event, usually in seconds. A writer
     * fed a slice of a partitioned stream may legitimately receive nothing for a long time, which
     * makes that window unbounded.</p>
     *
     * <p>Repeating the line is the whole fix. It changes no control flow — throwing from the timer
     * would end the timer thread, and the age-deadline flush is exactly what must keep running.</p>
     */
    private void reportOutstandingFlushFailure() {
        final Throwable t = flushFailure;
        if (t == null) {
            return;
        }
        // Retried on every tick until it succeeds, not attempted only when the failure is first
        // recorded: the platform may not have attached this component's flow yet at that moment,
        // and a failure that stands still has to reach the application manager. escalateFlushFailure
        // latches on success, so this costs one volatile read once it has been told.
        escalateFlushFailure(t);
        final long ageSeconds = Math.max(0L, (now() - flushFailedAtMillis) / 1000L);
        // Backed off, because BatchPolicy allows Interval:1 and this fires on every tick for as long
        // as the failure stands -- one line per second, forever, is a flood that buries the signal
        // it exists to raise. Doubling from the first repeat keeps the early ones close together,
        // where an operator is most likely to be looking, and thins out after that.
        if (reportsMade > 0 && ageSeconds < nextReportAtSeconds) {
            return;
        }
        nextReportAtSeconds = ageSeconds == 0 ? 1L : ageSeconds * 2L;
        reportsMade++;
        logger.logError(() -> "an age-deadline flush failed " + ageSeconds + "s ago and this writer "
                + "has applied nothing since; its positions are pinned and the application "
                + "checkpoint cannot advance past them. This will keep being reported until the "
                + "writer is restarted: " + detail(t));
    }

    /**
     * Rethrows an earlier age-deadline failure on a thread the platform owns.
     *
     * <p>Wrapped rather than rethrown bare so the stack shows both where it surfaced and where it
     * happened; the original is the cause.</p>
     */
    private void failIfFlushFailed() throws Exception {
        final Throwable t = flushFailure;
        if (t != null) {
            throw new IllegalStateException("an earlier age-deadline flush failed and this writer "
                    + "cannot continue; the batch it drained was never applied", t);
        }
    }

    /**
     * Cancels the age-deadline timer and releases the subclass's own resources. Idempotent.
     *
     * <p><b>{@link #cleanup()} is called from here, and that is not decoration.</b>
     * {@code RetriableWriter} declares {@code cleanup()} abstract, so every writer implements it —
     * but nothing ever called it. {@code Target} invokes {@code close()} on shutdown, and
     * {@code RetriableWriter.close()} only logs and flips {@code isClosed}/{@code inRetryLoop}; it
     * does not delegate. The built-in {@code DatabaseWriter} papers over that by overriding
     * {@code close()} to call its own {@code cleanup()}. Leaving each sample writer to rediscover
     * that is the per-module divergence that carries the highest
     * risk — and the symptom is silent: a JDBC connection leaked on every undeploy, with the
     * subclass's teardown code sitting there looking correct.</p>
     *
     * <p>The batch is deliberately <b>not</b> flushed here. Anything still accumulated was never
     * acked, so its positions stay pinned and the source replays it — correct by recovery, and
     * safer than a last-gasp write during shutdown.</p>
     *
     * <p>{@code super.close()} runs even if {@code cleanup()} throws: it clears
     * {@code inRetryLoop}, which is what stops the retry loop.</p>
     */
    @Override
    public void close() throws Exception {
        if (flushTimer != null) {
            flushTimer.cancel();
            flushTimer = null;
        }
        try {
            if (!cleanedUp) {
                cleanedUp = true;
                cleanup();
            }
        } finally {
            super.close();
        }
    }

    // ---------------------------------------------------------------- subclass contract

    /**
     * Applies the batch to the target and makes it durable. Must not return until committed,
     * because the caller acks — and therefore unpins — the moment it does.
     *
     * <p>The checkpoint row belongs inside this same transaction. That is what makes one apply plus
     * its checkpoint a single atomic recovery unit.</p>
     */
    protected abstract void applyBatch(List<WriteItem> batch) throws Exception;

    /**
     * The connection retry policy, read from the same property the built-in {@code DatabaseWriter}
     * reads.
     *
     * <p><b>This is implemented here because the obvious per-writer implementation is a latent
     * NPE.</b> {@code RetriableWriter} declares this abstract and then dereferences the result on
     * connection failure — {@code retryPolicy.getRetryWait()} and {@code getMaxRetries()} — so a
     * writer that returns null works perfectly until the target goes away, and then fails inside
     * the recovery path instead of recovering. Leaving each dialect writer to rediscover that is
     * exactly the per-module divergence that carries the highest risk.</p>
     *
     * <p>{@code parseRetryPolicy} supplies the platform's own default when the property is absent,
     * so this never returns null. The property name matches the built-in deliberately — a
     * typo in that literal would silently ignore the user's configuration and retry on the default
     * instead, which is why a test pins it against a real {@code Property}.</p>
     *
     * <p><b>It can throw.</b> {@code parseRetryPolicy} raises {@link IllegalArgumentException} on a
     * malformed policy — a non-numeric interval, or a non-positive {@code maxRetries} or
     * {@code retryInterval}. That is the correct failure and a subclass must not catch it: it
     * surfaces at {@code init}, where a misconfiguration is still cheap, rather than at the first
     * outage.</p>
     *
     * <p>The null-{@code prop} branch below is defensive only. {@code RetriableWriter.init}
     * constructs a {@code Property} before calling this, so it is unreachable in production.</p>
     */
    @Override
    public RetryPolicy getRetryPolicy(final Property prop) {
        if (prop == null) {
            return new RetryPolicy(DEFAULT_RETRY_WAIT_MILLIS, DEFAULT_MAX_RETRIES);
        }
        return prop.parseRetryPolicy("ConnectionRetryPolicy");
    }

    /**
     * Whether the platform should discard this event rather than deliver it.
     *
     * <p>False here: a writer with no distribution scheme has nothing to discard. The built-in
     * returns true only for events routed to its discard distribution id, which is a
     * multi-threaded-writer concern. A writer overrides this when key-invariant partitioning
     * arrives.</p>
     */
    @Override
    public boolean isDiscarded(final Event event) {
        return false;
    }

    /**
     * The position durably committed so far, or null if nothing is. Returned to the platform from
     * {@link #getEndpointCheckpoint()} and used to drop duplicates on replay — not to rewind.
     */
    protected abstract Position durablePosition() throws Exception;

    /** Tag this writer's log lines carry. Override to name the concrete adapter. */
    protected String loggerTag() {
        return getClass().getSimpleName();
    }

    /** Reads the standard {@code EnableLogging} property. */
    protected boolean debugLoggingEnabled(final Map<String, Object> properties) {
        final Object v = properties == null ? null : properties.get("EnableLogging");
        return v != null && Boolean.parseBoolean(v.toString());
    }

    /** The shared logger. Never null. */
    protected final Logger logger() {
        return logger;
    }

    /** Substitute the logger, for a subclass that builds its own or a test that captures output. */
    protected final void setLogger(final Logger replacement) {
        if (replacement != null) {
            this.logger = replacement;
        }
    }

    /** Override to source from the {@code BatchPolicy} property. */
    protected int batchEventCount() {
        return DEFAULT_BATCH_EVENTS;
    }

    /** Override to source from the {@code BatchPolicy} property. */
    protected long batchIntervalMillis() {
        return DEFAULT_BATCH_INTERVAL_MS;
    }

    // ---------------------------------------------------------------- RetriableWriter

    @Override
    public final void processEvent(final int channel, final Event event, final ImmutableStemma pos)
            throws Exception {
        failIfFlushFailed();
        if (accumulator.add(accept(new WriteItem(channel, event, pos)), now())
                && accumulator.size() >= holdUntilSize) {
            flushCompleted();
        }
    }

    /**
     * Sees every item on arrival, in stream order, before it joins the window. The default is
     * the identity; a writer that must remember something about the order of arrival (a
     * transaction opened by a marker, say) records it here and returns the item.
     */
    protected WriteItem accept(final WriteItem item) {
        return item;
    }

    /**
     * After a held window, the size at which the cut is asked again: one more window's worth.
     * Asking on every event past the count would scan the whole overflow each time; the age
     * deadline still asks on its own clock in between.
     */
    private int holdUntilSize;

    /**
     * Which items of a due window must be HELD. Given everything accumulated, in
     * arrival order, returns the indices that may not be applied yet; everything else is applied
     * now, whichever source it came from, and the held items overflow until they may. The default
     * holds nothing. A writer that must never split a source transaction holds the items of each
     * transaction still open — and only those, so a quiet reader's open transaction never dams
     * another reader's completed ones.
     *
     * <p>{@link #flush()} and an idle age deadline ignore the hold, since nothing more is coming
     * to complete an open run — see {@link #flushNow()}.</p>
     */
    protected java.util.BitSet hold(final List<WriteItem> accumulated) {
        return new java.util.BitSet();
    }

    /**
     * Applies what {@link #hold} does not hold, keeping the rest accumulating. Called on a
     * count- or age-triggered flush while events are still arriving.
     */
    protected final int flushCompleted() throws Exception {
        if (accumulator == null) {
            return 0;
        }
        final List<WriteItem> snapshot = accumulator.snapshot();
        final java.util.BitSet held = hold(snapshot);
        if (held.cardinality() >= accumulator.size()) {
            holdUntilSize = accumulator.size() + accumulator.maxItems();
            onWindowHeld(accumulator.size());
            return 0;
        }
        holdUntilSize = 0;
        // The platform's replay buffer is cleared by INDEX, everything at or below the newest
        // applied event. A held item that arrived before that event would be cleared with it
        // while still waiting here -- and a reconnect replaces this accumulator, so it would be
        // gone from both places. Clear only through the newest applied item that precedes every
        // held one; the buffer keeps the rest and replays it on reconnect.
        final int firstHeld = held.nextSetBit(0);
        clearThrough = null;
        for (int i = firstHeld - 1; i >= 0; i--) {
            if (!held.get(i)) {
                clearThrough = snapshot.get(i);
                break;
            }
        }
        clearThroughSet = firstHeld >= 0;   // nothing held: the batch's last item, as always
        try {
            return applyAndAck(accumulator.drainExcept(held, now()));
        } finally {
            clearThroughSet = false;
            clearThrough = null;
        }
    }

    /** Set by {@link #flushCompleted} for {@link #ackBatch}: the newest event the replay buffer may drop. */
    private WriteItem clearThrough;
    private boolean clearThroughSet;

    /**
     * A due window was held back whole because its only transaction is still open. Logged at
     * INFO once the overflow passes ten windows, so a transaction that never closes is visible.
     */
    protected void onWindowHeld(final int held) {
        final int window = accumulator.maxItems();
        final int windows = held / window;
        if (windows >= 10 && windows > heldWindowsLogged) {
            heldWindowsLogged = windows;
            logger.logAlways(() -> "window held at " + held + " events: an open source transaction "
                    + "has overflowed " + windows + " windows and is still accumulating; it is "
                    + "applied whole when it completes");
        }
    }

    /** The overflow multiple last logged, so each multiple past ten is said once. */
    private int heldWindowsLogged;

    @Override
    public final void processEvent(final int channel, final Event event) throws Exception {
        processEvent(channel, event, null);
    }

    @Override
    public Position getEndpointCheckpoint() throws Exception {
        return durablePosition();
    }

    // ---------------------------------------------------------------- flushing

    /**
     * The platform's flush hook. {@code BaseProcess.flush()} defaults to
     * {@code "Default flush operation takes no action"}; the built-in {@code DatabaseWriter}
     * overrides it to drain its buffer, and so do we. Overriding the existing hook rather than
     * inventing a parallel one means the platform's own flush points already reach our batch.
     */
    @Override
    public void flush() throws Exception {
        failIfFlushFailed();
        // ⚠ §151. Takes the monitor the age-deadline tick takes. Without it this was the ONE
        // unprotected way into flushNow: processEvent is called by RetriableWriter.receive under
        // synchObject, and runFlushTick locks it explicitly, but flush() -- which the platform
        // reaches through Target.flush() -- held nothing. Both drain the same BatchAccumulator,
        // which has no synchronisation of its own, and drain() reads `items` then assigns a fresh
        // list: two threads across those statements take the SAME batch and apply it twice.
        // Reentrant, so a platform that flushes from the receive thread is unaffected.
        synchronized (synchObject) {
            warnIfHeld("the platform asked for everything");
            flushNow();
        }
    }

    /** Says so when a flush that ignores the hold is about to take part of an open transaction. */
    private void warnIfHeld(final String why) {
        if (accumulator == null || accumulator.isEmpty()) {
            return;
        }
        final List<WriteItem> snapshot = accumulator.snapshot();
        final java.util.BitSet held = hold(snapshot);
        if (!held.isEmpty()) {
            final List<WriteItem> taken = new ArrayList<>(held.cardinality());
            for (int i = held.nextSetBit(0); i >= 0; i = held.nextSetBit(i + 1)) {
                taken.add(snapshot.get(i));
            }
            logger.logWarn(() -> "flush takes " + taken.size() + " event(s) of source "
                    + "transaction(s) still open (" + why + "): they are applied as far as they "
                    + "have arrived, and any remainder lands in its own later commit");
            onFlushPastHold(taken);
        }
    }

    /**
     * A flush is about to apply these held items -- part of transactions still open -- because
     * the stream went quiet or the platform asked. A writer that remembers applied transactions
     * must not count these as complete: their remainder may still arrive.
     */
    protected void onFlushPastHold(final List<WriteItem> taken) {
    }

    /**
     * Applies and acks whatever has accumulated, returning the count. Safe to call when empty.
     * Call from a timer as well as on add: a batch that stops receiving events before reaching its
     * count would otherwise sit indefinitely with its positions pinned, stalling the app checkpoint.
     */
    protected final int flushNow() throws Exception {
        if (accumulator == null) {
            return 0;   // flush() or close() before a successful init(); nothing accumulated yet
        }
        return applyAndAck(accumulator.drain());
    }

    private int applyAndAck(final List<WriteItem> batch) throws Exception {
        holdUntilSize = 0;
        heldWindowsLogged = 0;
        final long startedAt = now();
        final int applied = WriteCycle.flush(batch, this::applyBatch, this::ackBatch);
        if (applied > 0) {
            // After WriteCycle.flush returns, so these describe work that is durable AND acked.
            // Counting on entry would report events the target never took.
            processed.addAndGet(applied);
            final long committedAt = now();
            lastCommit = new CommitStats(applied, committedAt, committedAt - startedAt);
        }
        return applied;
    }

    /**
     * Reports this writer's throughput to the platform, so a Target shows a count rather than
     * nothing at all.
     *
     * <p><b>Measured:</b> without this the monitor and preview show no events for a writer that is
     * applying correctly, because the platform has no other way to learn what a Target did — its
     * output is a database, not a stream. The built-in {@code DatabaseWriter} overrides this same
     * method; this module never did.</p>
     *
     * <p>{@code super} first: {@code RetriableWriter} publishes its own discarded-event counters,
     * and skipping it would trade one blind spot for another.</p>
     *
     * @param events the collection to add to
     */
    @Override
    public void publishMonitorEvents(final MonitorEventsCollection events) {
        super.publishMonitorEvents(events);
        events.add(MonitorEvent.Type.PROCESSED, processed.get());
        final CommitStats commit = lastCommit;   // read ONCE: every metric below is that commit's
        if (commit != null) {
            // One apply is one transaction and one commit, so the writer's "IO" and its "commit"
            // are the same event; the built-in reports both because its batch and its commit are
            // separate. Reporting the same number under both keeps a dashboard written against the
            // built-in readable rather than half empty.
            events.add(MonitorEvent.Type.LAST_COMMIT_TIME, commit.millis);
            events.add(MonitorEvent.Type.LAST_IO_TIME, commit.millis);
            events.add(MonitorEvent.Type.TOTAL_EVENTS_IN_LAST_COMMIT, commit.count);
            events.add(MonitorEvent.Type.TOTAL_EVENTS_IN_LAST_IO, commit.count);
            events.add(MonitorEvent.Type.COMMIT_LATENCY, commit.latencyMillis);
            events.add(MonitorEvent.Type.EXTERNAL_IO_LATENCY, commit.latencyMillis);
        }
        final String position = commitPositionText();
        if (position != null) {
            events.add(MonitorEvent.Type.TARGET_COMMIT_POSITION, position);
        }
    }

    /**
     * The durable position, rendered the way the built-in renders it: each path's low source
     * position, joined by {@code |}.
     *
     * <p>Never throws — this runs on the platform's monitoring thread, and a writer that cannot
     * describe its position must still report the counts it can.</p>
     *
     * @return the rendered position, or null when there is none to report
     */
    private String commitPositionText() {
        try {
            final Position durable = durablePosition();
            if (durable == null) {
                return null;
            }
            final StringBuilder out = new StringBuilder();
            for (final com.webaction.recovery.Path path : durable.values()) {
                if (out.length() > 0) {
                    out.append('|');
                }
                out.append(path.getLowSourcePosition());
            }
            return out.length() == 0 ? null : out.toString();
        } catch (final Throwable t) {
            logger.log(() -> "could not render the durable position for monitoring: " + t);
            return null;
        }
    }

    /** True when a flush is due on time alone. For a subclass timer to poll. */
    protected final boolean isFlushDue() {
        return accumulator != null && accumulator.isDue(now());
    }

    /** Items accumulated but not yet flushed; {@code 0} when no accumulator is configured. */
    protected final int pendingCount() {
        return accumulator == null ? 0 : accumulator.size();
    }

    /**
     * Releases the checkpoint pin for a batch already durable.
     *
     * <p>{@code receiptCallback} is null-guarded because it genuinely can be null — measured null
     * at both {@code init} and {@code processEvent} for a writer that did not implement
     * {@code Acknowledgeable}. The built-in guards every call site the same way. This class does
     * implement the marker, so the callback is expected; the guard keeps a misconfiguration from
     * turning into an NPE inside the commit path.</p>
     *
     * <p><b>Deliberate divergence from the built-in.</b> It acks once per commit with a count and a
     * single merged position — {@code receiptCallback.ack(eventsInLastCommit, lastAckedPosition)}.
     * We ack each item's own position instead. A batch can carry positions from several source
     * keys, and a merged stemma is a lattice join rather than a total order, so acking only the
     * last position risks under-releasing the others. Per-item is the safe reading of a contract we
     * have not fully characterised. It costs one call per event, which is worth revisiting once
     * perf work can measure it — tracked as a known cost, not an oversight.</p>
     *
     * <p><b>⚠ Every event is acked, positioned or not (T2b-4, §43.24a).</b> This loop used to ack
     * ONLY items carrying a position. With recovery disabled the position is null for every event,
     * so it acked nothing — and the platform's {@code eventsAcked}, which is what the console shows
     * as <i>Total output</i>, never moved. A writer applying rows correctly reported 0 for a whole
     * live run. None of the six shipped examples declares {@code RECOVERY}, so that is the
     * configuration users actually watch.</p>
     *
     * <p><b>Counting delivery and reporting a recovery position are two different acts.</b> The
     * count is always true; the position exists only under recovery. So the unpositioned items are
     * counted in one call and the positioned ones keep releasing their own pins.</p>
     *
     * <p>⚠ The remainder is counted, NOT {@code batch.size()}, and the difference matters. Every
     * overload of {@code ack} funnels into {@code ack(int, Collection)} and adds its count to the
     * same {@code eventsAcked} — so acking the full size here as well as per position would report
     * DOUBLE under recovery. §43.24a's recorded fix said to ack {@code batch.size()}
     * unconditionally and keep the positional acks; that is what this paragraph exists to correct.
     * Each event is counted exactly once, with or without recovery.</p>
     */
    private void ackBatch(final List<WriteItem> batch) throws Exception {
        if (receiptCallback != null) {
            int unpositioned = 0;
            for (final WriteItem item : batch) {
                if (item.position() != null) {
                    receiptCallback.ack(1, item.position());
                } else {
                    unpositioned++;
                }
            }
            if (unpositioned > 0) {
                receiptCallback.ack(unpositioned);
            }
        }
        // Releases the in-flight window RetriableWriter replays on reconnect. Monotonic:
        // clear(index) discards everything at or below it, so this must be the newest event
        // that no held item precedes (see flushCompleted); with nothing held it is the last.
        final WriteItem last = clearThroughSet ? clearThrough : batch.get(batch.size() - 1);
        if (eventBuffer != null && last != null && last.event() != null) {
            eventBuffer.clear(last.event());
        }
    }

    /** Overridable so tests can drive the age deadline without sleeping. */
    protected long now() {
        return System.currentTimeMillis();
    }
}
