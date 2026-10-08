package com.example.common;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

import com.webaction.recovery.ImmutableStemma;
import com.webaction.runtime.components.openprocessor.StriimOpenProcessor;
import com.webaction.runtime.containers.TaskEvent;
import com.webaction.runtime.containers.WAEvent;

/**
 * On the deployed platform, {@code StriimOpenProcessor.send(Object)} already attaches an
 * {@link ImmutableStemma} — the recovery/checkpoint marker — when recovery is enabled. But it
 * attaches {@code ImmutableStemma.batchWidePosition(getAdded())}, a position merged across the
 * WHOLE inbound batch, not the specific source event's own position.
 *
 * <p>That over-reports: emitting event 1 of a 100-event batch checkpoints through the batch's
 * high-water position, so a crash before event 100 is processed loses events 2..100 on restart.
 * This class instead attaches the exact source container's own position, which
 * {@code send(Object)} has no way to do since it only ever sees the transformed payload, not the
 * container it came from.</p>
 *
 * <p>Routes through {@code send(ITaskEvent)} — the same path {@code send(Object)} uses — rather
 * than {@code send(List, List)}: the latter reaches {@code OpenProcessor.doOutput(List, List)},
 * whose first statement unconditionally calls {@code dumpOutput(...)} and dumps every event's full
 * payload to stdout, because {@code traceOptions} is hardcoded with its trace flag on and never
 * reset. {@code doOutput(ITaskEvent)} has no such side effect.</p>
 *
 * <p>Single home for this so every OP — whether it extends {@link AbstractOpenProcessorApp} or
 * {@code StriimOpenProcessor} directly — calls the same code instead of a locally duplicated
 * copy.</p>
 */
public final class PositionPropagation {

    private PositionPropagation() {
    }

    /**
     * Sends {@code event} downstream carrying {@code stemma} as its outer container's position.
     * {@code stemma} may be null — no upstream checkpointing, for instance — and that is carried
     * through as-is. {@code event == null} is a no-op, matching {@code send(Object)}'s own null
     * guard.
     */
    public static void sendWithPosition(StriimOpenProcessor op, com.webaction.proc.events.WAEvent event,
            ImmutableStemma stemma) throws Exception {
        if (event == null) {
            return;
        }
        WAEvent outer = new WAEvent(event, stemma);
        op.send(TaskEvent.createStreamEvent(Collections.singletonList(outer)));
    }

    /**
     * Sends a whole GROUP — everything one source event produced — as ONE {@code TaskEvent}, every
     * member carrying {@code stemma}. Null members are skipped, as {@link #sendWithPosition} skips
     * a null event; an empty or null group is a no-op.
     *
     * <p><b>Why one send rather than N (design §5.2).</b> A fan-out sent one member at
     * a time can fail on member 3 of 5 and leave 1–2 permanently downstream while 3–5 are replayed:
     * a partial group that the position coordinate system cannot describe, because all N share P.
     * One {@code TaskEvent} is all-or-nothing at the emission boundary — the platform's
     * {@code doOutput(ITaskEvent)} hands the whole batch to ONE {@code Channel.publish} call
     * (it also stamps the lee entry on the
     * batch's FIRST event only, exactly as it does for its own batch-wide {@code send(Object)}) —
     * and it is N× fewer output calls. <b>One visible side effect:</b> {@code doOutput} increments
     * the platform's {@code outputTotal} once per CALL, so the OUTPUT / OUTPUT_RATE monitor
     * figures for a fan-out OP now count groups, not events (a 1→4 fan-out reports +1 where it
     * reported +4). It does NOT make the TARGET atomic: the writer still receives N
     * events and commits on its own policy, which is what {@code TransactionScope} and
     * {@code PreserveSourceTransactionBoundary} are for (design §8.1). Still {@code send(ITaskEvent)},
     * never {@code send(List, List)}, for the {@code dumpOutput} reason above.</p>
     */
    public static void sendAllWithPosition(StriimOpenProcessor op,
            List<? extends com.webaction.proc.events.WAEvent> events, ImmutableStemma stemma) throws Exception {
        if (events == null || events.isEmpty()) {
            return;
        }
        List<WAEvent> outer = new ArrayList<>(events.size());
        for (com.webaction.proc.events.WAEvent event : events) {
            if (event != null) {
                outer.add(new WAEvent(event, stemma));
            }
        }
        if (outer.isEmpty()) {
            return;
        }
        op.send(TaskEvent.createStreamEvent(outer));
    }

    /**
     * {@link #sendAllWithPosition} for a group whose members carry DIFFERENT positions — a batch
     * of several source events' outputs released together (see
     * {@link SyntheticTransactionBoundaries}). Still ONE {@code TaskEvent}, for the same
     * all-or-nothing reason; each member's container carries its own source's position, so the
     * writer's checkpoint after the batch's {@code COMMIT} is the highest of them and a replay
     * re-sends nothing that committed.
     */
    public static void sendAllWithPositions(StriimOpenProcessor op,
            List<SyntheticTransactionBoundaries.Positioned> events) throws Exception {
        if (events == null || events.isEmpty()) {
            return;
        }
        List<WAEvent> outer = new ArrayList<>(events.size());
        for (SyntheticTransactionBoundaries.Positioned p : events) {
            if (p != null && p.event() != null) {
                outer.add(new WAEvent(p.event(), p.position()));
            }
        }
        if (outer.isEmpty()) {
            return;
        }
        op.send(TaskEvent.createStreamEvent(outer));
    }
}
