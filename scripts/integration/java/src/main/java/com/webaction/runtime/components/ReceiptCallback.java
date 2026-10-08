package com.webaction.runtime.components;

import java.util.Collection;

import com.webaction.recovery.Position;
import com.webaction.recovery.Stemma;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.ReceiptCallback} — how a Target
 * releases the checkpoint pin on data it has made durable.
 *
 * <p><b>This interface is a Target's third observable output.</b> A Target emits no events, so
 * what a tier case can see is the target database, the checkpoint row, and what was acked; this is
 * the seam for the third. {@code TargetCore} injects a recording implementation, so a case can
 * assert both the acked COUNT and the positions released.</p>
 *
 * <p>{@code AbstractWriterApp.ackBatch} calls {@code ack(int, Stemma)} per item; the platform's
 * built-in {@code DatabaseWriter} calls {@code ack(int, Position)} once per commit. Both are
 * declared so either writer drives this mock unchanged. The count-only {@code ack(int)} supplies the console
 * output count even without a recovery position, so both paths are assertable.</p>
 */
public interface ReceiptCallback {

    /** Releases {@code count} events against a collection of positions. */
    void ack(int count, Collection<Stemma> positions);

    /** Releases {@code count} events with no position — the no-recovery path. */
    default void ack(int count) {
        ack(count, java.util.Collections.emptyList());
    }

    /** Releases {@code count} events against one position. */
    default void ack(int count, Stemma position) {
        ack(count, java.util.Collections.singletonList(position));
    }

    /** Releases {@code count} events against every path in {@code position}. */
    default void ack(int count, Position position) {
        java.util.List<Stemma> stemmas = new java.util.ArrayList<>();
        if (position != null) {
            for (com.webaction.recovery.Path path : position.values()) {
                stemmas.add(new com.webaction.recovery.ImmutableStemma(path.getComponentID(),
                        path.getDistributionID(), path.getLowSourcePosition(),
                        path.getHighSourcePosition()));
            }
        }
        ack(count, stemmas);
    }

    /** As {@link #ack(int, Position)}, and the platform additionally persists the checkpoint. */
    default void ackAndPersist(int count, Position position) {
        ack(count, position);
    }
}
