package com.example.common;

import java.util.List;

/**
 * Enforces the one ordering a recoverable Striim writer must never get wrong:
 * <b>apply-and-commit fully completes before anything is acked.</b>
 *
 * <p>Why this is a class rather than three lines inline. A Target pins every position it has
 * received and not yet acked, in {@code Target.memoryCheckpoint}, and that pin is what holds the
 * app checkpoint back. The ack is what releases it. So acking before the commit is durable moves
 * the checkpoint past data that is not on disk, and after a crash the source resumes beyond it —
 * silently, because every component reports healthy. The recovery design document sets out the
 * whole ordering.</p>
 *
 * <p>The pin only exists at all if the adapter implements {@code com.webaction.recovery.Acknowledgeable}
 * — an empty marker interface. Measured: without it {@code receiptCallback} is never injected and
 * no pinning happens. {@code AbstractWriterApp} declares it; a writer that bypasses that base must
 * declare it too.</p>
 *
 * <p>Platform-free so the invariant can actually be tested: the shell that uses it cannot be
 * constructed outside a running server.</p>
 */
public final class WriteCycle {

    /** Applies a batch to the target and makes it durable. Must not return until committed. */
    @FunctionalInterface
    public interface Apply<T> {
        void apply(List<T> batch) throws Exception;
    }

    /** Releases the checkpoint pin for a batch already known to be durable. */
    @FunctionalInterface
    public interface Ack<T> {
        void ack(List<T> batch) throws Exception;
    }

    private WriteCycle() {
    }

    /**
     * Runs one write cycle: apply, then ack. An empty batch does nothing — notably it does not ack,
     * since there is no pin to release.
     *
     * <p>If {@code apply} throws, {@code ack} is <b>not</b> called and the exception propagates. The
     * positions stay pinned, the app checkpoint stays where it was, and the platform's retry or
     * recovery path sees the batch again. That is the correct outcome: at-least-once with the
     * duplicates filtered on replay, rather than at-most-once with a silent gap.</p>
     *
     * @return the number of items acked
     */
    public static <T> int flush(final List<T> batch, final Apply<T> apply, final Ack<T> ack)
            throws Exception {
        if (batch == null || batch.isEmpty()) {
            return 0;
        }
        apply.apply(batch);   // must be durable before the next line runs
        ack.ack(batch);
        return batch.size();
    }
}
