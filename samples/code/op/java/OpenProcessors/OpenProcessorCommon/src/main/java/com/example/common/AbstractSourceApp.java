package com.example.common;

import com.webaction.event.Event;
import com.webaction.recovery.SourcePosition;

/**
 * The {@code SourcePosition} recovery family's reader shell: the two framework
 * calls only a live {@code SourceProcess} can make, behind {@link SourceChannel} so a core can be
 * driven with neither.
 *
 * <p>This is the counterpart to {@link AbstractCheckpointSourceApp}, which serves the
 * {@code ComponentCheckpoint} family. Both extend {@link AbstractReaderApp}, so every reader —
 * whichever recovery family it belongs to, and including ones not yet written — inherits the same
 * flow-identity pattern rather than re-deriving it.</p>
 *
 * <p><b>Deliberately NOT here: {@code init}, {@code receiveImpl} and {@code close}.</b> Measured
 * across the modules in this family before the class was written, and the differences are real
 * rather than incidental.
 * The differences: their {@code init} bodies diverge after {@code super.init(...)};
 * {@code receiveImpl} differs in whether the inter-tick sleep sits in a {@code finally}; and
 * {@code close()} is {@code synchronized} in one and not the other. Forcing those into one shape
 * is what once left {@link Emitter} shipping with no consumers at all while every reader
 * hand-rolled its own; see its javadoc for who has adopted it since.</p>
 *
 * <p>A module that declares a {@code SourceChannel} of its own should derive {@code emit} and
 * {@code checkpoint} from {@link SourceChannel} rather than declaring them again. A
 * {@code ComponentCheckpoint} reader belongs to {@link AbstractCheckpointSourceApp} instead.</p>
 *
 * <p><b>Testability.</b> This class IS constructible in a test: a test subclass can extend it
 * and drive it. An earlier
 * version of this note said "compile-verified only, like every {@code SourceProcess} subclass",
 * which was false: the {@code NoClassDefFoundError} behind that claim was a missing {@code jeromq}
 * dependency in this module's pom, not a property of the platform.</p>
 *
 * <p>{@code emit} and {@code checkpoint} are each a single framework call, and {@code emit}'s
 * channel argument is covered — a mutation from channel 0 to 1 changes which sink receives, and a
 * test pins it. {@code checkpoint} delegates to {@code setSourcePosition}, which is an empty
 * method in the platform; there is nothing observable to assert, and saying so is
 * better than implying coverage that could not exist.</p>
 *
 * @param <P> this reader's {@code SourcePosition} subclass
 * @param <E> the event type it emits — a parameter, not {@code Event}, because a core that emits
 *            {@code WAEvent} must not have to widen its own seam to adopt this shell
 */
public abstract class AbstractSourceApp<P extends SourcePosition, E extends Event>
        extends AbstractReaderApp implements SourceChannel<P, E> {

    /** The output channel index every reader emits on. */
    private static final int DEFAULT_CHANNEL = 0;

    /**
     * Hands one built event downstream.
     *
     * <p>Throws through to the caller, which aborts the current cycle — the contract
     * {@link Emitter} declares and its current consumers already implement — including the one
     * that reaches it by extending this class rather than by importing it.</p>
     */
    @Override
    public void emit(E event) throws Exception {
        send(event, DEFAULT_CHANNEL);
    }

    /**
     * Offers {@code position} to the framework checkpoint.
     *
     * <p>Unchecked, matching {@link SourceChannel#checkpoint}: framework checkpoint I/O failures
     * are internal, and callers checkpoint from methods that declare no checked exception.</p>
     */
    @Override
    public void checkpoint(P position) {
        setSourcePosition(position);
    }
}
