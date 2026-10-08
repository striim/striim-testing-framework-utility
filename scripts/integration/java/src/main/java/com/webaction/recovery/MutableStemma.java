package com.webaction.recovery;

import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.recovery.MutableStemma} — the accumulating side of
 * the pair, which a writer merges each acked position into.
 *
 * <p>Merging is a per-key {@code max} over {@link SourcePosition}'s natural order; see
 * {@link Stemma} for what that reduction does and does not model.</p>
 */
public class MutableStemma extends Stemma {

    private static final long serialVersionUID = 1L;

    private SourcePosition low;
    private SourcePosition high;

    /** An empty accumulator for one (component, distribution) key. */
    public MutableStemma(UUID componentID, String distributionID) {
        super(componentID, distributionID);
    }

    /** A copy of {@code source}, sharing its identity and its current marks. */
    public MutableStemma(Stemma source) {
        super(source.getComponentID(), source.getDistributionID());
        this.low = source.getLowSourcePosition();
        this.high = source.getHighSourcePosition();
    }

    @Override
    public SourcePosition getLowSourcePosition() {
        return low;
    }

    @Override
    public SourcePosition getHighSourcePosition() {
        return high;
    }

    /**
     * Raises this stemma's marks to {@code other}'s where {@code other} is higher.
     *
     * <p>Monotone by construction: a position already merged can never be lowered by a later
     * merge, which is the property a writer's durable position depends on.</p>
     *
     * @return 1 when either mark moved, 0 when nothing did
     */
    public int mergeHigher(Stemma other) {
        if (other == null) {
            return 0;
        }
        SourcePosition newLow = higher(low, other.getLowSourcePosition());
        SourcePosition newHigh = higher(high, other.getHighSourcePosition());
        boolean moved = newLow != low || newHigh != high;
        low = newLow;
        high = newHigh;
        return moved ? 1 : 0;
    }

    private static SourcePosition higher(SourcePosition a, SourcePosition b) {
        if (a == null) {
            return b;
        }
        if (b == null) {
            return a;
        }
        return a.compareTo(b) >= 0 ? a : b;
    }

    /** An immutable snapshot of this accumulator's current marks. */
    public ImmutableStemma toImmutable() {
        return new ImmutableStemma(getComponentID(), getDistributionID(), low, high);
    }
}
