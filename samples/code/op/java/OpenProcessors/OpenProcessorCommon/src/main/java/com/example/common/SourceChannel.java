package com.example.common;

import com.webaction.event.Event;
import com.webaction.recovery.SourcePosition;

/**
 * A reader's full output seam: emit events, and record the position the framework should recover
 * from. The two calls only a live {@code SourceProcess} can make, behind one interface so a core
 * can be driven with neither.
 *
 * <p><b>Two type parameters, not one.</b> The original design specified {@code SourceChannel<P
 * extends SourcePosition> extends Emitter}, which does not do what it reads as: with a flat
 * {@code Emitter} declaring {@code emit(Event)}, a Spanner channel emitting
 * {@code WAEvent} would have to implement <i>both</i> signatures. The element type has to be a
 * parameter as well, so the position and the event vary independently — {@code SpannerSourcePosition}
 * with {@code WAEvent}, and a future
 * {@code JsonNodeEvent} reader without a new interface.</p>
 *
 * <p>{@code checkpoint} is separate from {@code emit} because the two have different
 * durability meanings: emitting hands an event downstream, checkpointing asserts that everything
 * up to a position is safely handed over. A core that conflates them cannot express
 * at-least-once.</p>
 */
public interface SourceChannel<P extends SourcePosition, E extends Event> extends Emitter<E> {

    /**
     * Records {@code position} as recoverable. The framework decides when it becomes durable, so
     * an implementation must treat this as "offer", never "flushed".
     *
     * <p><b>No {@code throws}, deliberately, and settled while there were no consumers.</b> Existing
     * implementations already declared it unchecked ("Never throws: framework checkpoint I/O
     * failures are internal"). Declaring {@code throws Exception}
     * here would be legal for them to narrow, but not for CALLERS:
     * {@code Processor.onInitialLoadTableComplete} and the initial-load emitter callback checkpoint
     * from methods that declare no checked exception, so adoption would push {@code throws
     * Exception} up the whole initial-load chain to satisfy a contract neither implementation
     * needs.</p>
     */
    void checkpoint(P position);
}
