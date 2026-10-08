package com.example.common;

/**
 * One commit's monitoring numbers, immutable so they can be published as a set.
 *
 * <p>{@link AbstractWriterApp} holds these behind a single volatile reference and replaces the
 * whole object per commit. Published as separate volatile fields, a read from the platform's
 * monitoring thread could interleave with a commit and report a count from one commit beside the
 * timestamp of another — a tuple that never existed.</p>
 *
 * <p>Top-level named class, not an inner one, per the OP classloader rule.</p>
 */
final class CommitStats {

    /** How many events the commit applied. */
    final long count;
    /** When it committed, in epoch milliseconds from the writer's clock. */
    final long millis;
    /** How long it took end to end, in milliseconds. */
    final long latencyMillis;

    CommitStats(final long count, final long millis, final long latencyMillis) {
        this.count = count;
        this.millis = millis;
        this.latencyMillis = latencyMillis;
    }
}
