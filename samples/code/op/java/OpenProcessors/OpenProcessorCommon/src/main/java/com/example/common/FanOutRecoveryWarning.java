package com.example.common;

import java.util.List;

import com.webaction.recovery.ImmutableStemma;
import com.webaction.proc.events.WAEvent;

/**
 * Warns, at most twice per component, when an OP emits MORE THAN ONE event for a single source
 * event while recovery is enabled — the precondition for the fan-out data loss described below
 * (fan-out into one recovery-enabled target).
 *
 * <p><b>Why a warning and not a fix.</b> The OP cannot fix this. All N emitted events necessarily
 * carry the source event's one {@link ImmutableStemma}, because no distinct value exists to give
 * them (Striim 5.4):</p>
 * <ul>
 *   <li>{@code AfterSourcePosition.compareTo} <b>delegates to the position it wraps</b>, so
 *       {@code After(P).compareTo(P) == 0}. It is a label, not an ordinal.</li>
 *   <li>{@code MutableStemma$DropDuplicatesStemmaProcessor.process} reads the FILTER's
 *       {@code getAtOrAfter()} to choose the filter's low or high bound, then compares it against
 *       only the event's {@code getLowSourcePosition()} and <b>drops on {@code >= 0}</b> — at or
 *       after, so equality drops. An event contributes exactly one value.</li>
 * </ul>
 * <p>Minting source-specific {@code SourcePosition} values would corrupt the reader's restart
 * mapping. So a target that commits a proper subset of the group records the shared position in
 * its {@code CHKPOINT}, and the events at that position which were never written are filtered out
 * of the replay rather than re-applied.</p>
 *
 * <p><b>Why a boundary sighting does NOT silence it.</b> Real source transaction boundaries are
 * the fix, and this class does notice them — but seeing one is necessary, not sufficient, so
 * suppressing on it would go quiet in the two most likely configurations:</p>
 * <ul>
 *   <li>{@code PreserveSourceTransactionBoundary} on the stock writer is {@code Boolean},
 *       <b>default {@code "false"}</b> (verified in {@code DatabaseWriter_1_0}'s
 *       {@code @PropertyTemplateProperty}). Boundaries on the stream plus a default writer is
 *       still unsafe, and that is the common case.</li>
 *   <li>An OP sits UPSTREAM of any CQ, and a CQ can strip {@code BEGIN}/{@code COMMIT} after this
 *       code has seen them — an observed field bug
 *       ({@code WHERE ... OperationName NOT IN ('COMMIT','BEGIN')}). So "this OP saw a boundary"
 *       does not mean "the writer saw a boundary".</li>
 * </ul>
 * <p>The OP also cannot see the writer's {@code CommitPolicy}. So the sighting changes the
 * MESSAGE — from "this cannot be atomic" to "this is atomic only if you also did these two
 * things" — rather than the decision to speak.</p>
 *
 * <p><b>⚠ The soft variant is not reachable for every OP, and the reason is worth knowing.</b>
 * Boundaries are detected on the EMITTED events, so an OP that DROPS a boundary keeps
 * {@code boundarySeen} false and only ever produces the strong message.
 * A routing OP may return without appending anything when
 * {@code metadata["TableName"]} is null or is absent from the configured mappings — and a boundary
 * synthesized by a converting OP may carry {@code TableName: null} on purpose. Such a
 * boundary never reaches the emitted list. A boundary from a stock CDC reader whose table IS
 * mapped does reach it, and takes the routing OP's forward-once path because
 * {@code validOperation} admits only INSERT/UPDATE/DELETE/SELECT. Erring toward the strong message
 * is the safe direction, so this is a documented limitation rather than a bug to fix here.</p>
 *
 * <p><b>Latching.</b> One line per message variant, so a 20 000-event run cannot produce 20 000
 * lines, and a stream whose boundaries appear only after the first fan-out still gets the
 * corrected, weaker message rather than being stuck with the first verdict. At most two ERROR
 * lines for the life of the component — not per {@code run()} call.</p>
 *
 * <p><b>Coverage is the {@code run()} batch loop only.</b> It is wired at
 * {@code AbstractConvertingOpenProcessorApp.run()}, which covers every OP that emits from there.
 * It does <b>not</b> see {@code emitAsync}, so a background flush that releases a whole
 * reassembled transaction on one position — a cache-eviction path, say —
 * is NOT covered. Do not read this class as complete coverage.</p>
 *
 * <p>Not thread-safe, and does not need to be: {@code com.webaction.runtime.components
 * .openprocessor.OpenProcessor} is {@code final} and holds a single {@code task} and a single
 * output channel, so emission is serial per component instance. A torn read here would at worst
 * duplicate one log line.</p>
 */
public final class FanOutRecoveryWarning {

    private final String component;
    private boolean boundarySeen;
    private boolean warnedNoBoundary;
    private boolean warnedWithBoundary;

    /** @param component name used to open the message; the OP's module/tag is the useful value. */
    public FanOutRecoveryWarning(String component) {
        this.component = component == null ? "OpenProcessor" : component;
    }

    /**
     * Call once per source event, with everything that event produced.
     *
     * @param emitted what {@code processEvent} returned; {@code null} or empty is a no-op
     * @param stemma  the source container's position — {@code null} means no upstream
     *                checkpointing, in which case the defect cannot apply and nothing is logged
     * @param logger  may be {@code null}, in which case nothing is logged
     */
    public void observe(List<? extends WAEvent> emitted, ImmutableStemma stemma, Logger logger) {
        // Every early return below is a state in which NOTHING can ever be logged, so the boundary
        // scan would be pure per-event waste on the hot path. Ordered before the scan for that
        // reason: recovery-off OPs are the majority and must pay nothing at all.
        if (emitted == null || emitted.isEmpty() || stemma == null || logger == null) {
            return;
        }
        // Both variants already said: this instance can never speak again, so stop scanning.
        if (warnedNoBoundary && warnedWithBoundary) {
            return;
        }
        // One pass: latch the boundary sighting AND measure the width as the number of DATA events.
        // Markers are not copies -- a lone COMMIT is width 0, and a group that
        // SyntheticTransactionBoundaries wrapped is N data events plus two markers, not N + 2.
        int dataEvents = 0;
        for (WAEvent e : emitted) {
            if (isTransactionBoundary(e)) {
                boundarySeen = true;
            } else {
                dataEvents++;
            }
        }
        if (dataEvents <= 1) {
            return; // not a fan-out
        }
        final int width = dataEvents;
        if (boundarySeen) {
            if (warnedWithBoundary) {
                return;
            }
            warnedWithBoundary = true;
            logger.logError(() -> component + ": RECOVERY FAN-OUT -- emitted " + width
                    + " events for ONE source event, all sharing that event's single recovery"
                    + " position. Transaction boundaries ARE present on this stream, which makes the"
                    + " group atomic -- but ONLY IF the target sets PreserveSourceTransactionBoundary:"
                    + " true (stock default is FALSE) and no CQ between here and the target filters"
                    + " BEGIN/COMMIT. This OP can see neither, so VERIFY BOTH. If either is missing,"
                    + " a commit landing inside the group records the shared position and the"
                    + " unwritten remainder is filtered out of the replay."
                    + " Verify transaction boundaries before using fan-out with recovery.");
        } else {
            if (warnedNoBoundary) {
                return;
            }
            warnedNoBoundary = true;
            logger.logError(() -> component + ": RECOVERY DATA-LOSS RISK -- emitted " + width
                    + " events for ONE source event, all sharing that event's single recovery"
                    + " position, and NO transaction boundary has been seen on this stream. Nothing"
                    + " can make the group atomic in that state: a target that commits part of it"
                    + " records the shared position, and the events at that position which were"
                    + " never written are then filtered out of the replay instead of being"
                    + " re-applied."
                    + " FIX: preserve real source transaction boundaries end to end -- reader"
                    + " FilterTransactionBoundaries: false, no CQ filtering BEGIN/COMMIT, target"
                    + " PreserveSourceTransactionBoundary: true with CommitPolicy '-1' (or a"
                    + " count-based policy with BatchPolicy EventCount:1)."
                    + " Where the source has none, use one target per destination table instead."
                    + " Verify transaction boundaries before using fan-out with recovery.");
        }
    }

    /** True once a {@code BEGIN}, {@code COMMIT} or {@code ROLLBACK} has been observed. Test seam. */
    public boolean boundarySeen() {
        return boundarySeen;
    }

    /**
     * Records that the stream WILL carry boundaries even though the group being observed does not
     * show them — a shell releasing several source events' groups as one framed batch observes
     * each group as its OP emitted it, and the markers are added at the release.
     */
    public void noteBoundaries() {
        boundarySeen = true;
    }

    /**
     * A transaction boundary is carried as {@code metadata["OperationName"]}, the same key
     * a routing OP's {@code validOperation} check uses to admit only INSERT/UPDATE/DELETE/
     * SELECT — so a boundary is exactly what that check rejects.
     */
    private static boolean isTransactionBoundary(WAEvent e) {
        if (e == null || e.metadata == null) {
            return false;
        }
        Object op = e.metadata.get("OperationName");
        if (op == null) {
            return false;
        }
        String s = op.toString();
        return "BEGIN".equals(s) || "COMMIT".equals(s) || "ROLLBACK".equals(s);
    }
}
