package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

import com.webaction.recovery.Stemma;
import com.webaction.runtime.components.ReceiptCallback;

/**
 * The acknowledgement seam, recording rather than releasing — a target's third observable output.
 *
 * <p>Counts and positions accumulate ACROSS a {@link TargetDriver#restart()}, deliberately: the
 * question a recovery case asks is how many events the target acked in total, and resetting on
 * restart would hide a double-write behind two half-counts.</p>
 */
final class RecordingReceiptCallback implements ReceiptCallback {

    private int events;
    private final List<String> positions = new ArrayList<>();

    @Override
    public void ack(int count, Collection<Stemma> acked) {
        events += count;
        if (acked != null) {
            for (Stemma stemma : acked) {
                positions.add(stemma == null ? "null" : stemma.toString());
            }
        }
    }

    /** Total events acknowledged. */
    int events() {
        return events;
    }

    /**
     * Every position released, in order.
     *
     * <p>A count-only {@code ack(int)} contributes nothing here while still raising
     * {@link #events()} — in the count-only path, where the console's "Total output" needs the count and the position path is never
     * taken. The two are reported separately so a case can tell those apart.</p>
     */
    List<String> positions() {
        return positions;
    }
}
