package com.webaction.recovery;

import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * Mock of the platform's {@code com.webaction.recovery.MultiMutableStemma} — a writer's running
 * "everything durably committed so far", one {@link MutableStemma} per (component, distribution).
 *
 * <p>This is the class {@code JdbcSinkV1A.App} accumulates into and converts back out of,
 * so its merge semantics are what the tier's recovery assertions rest on. Merging is a per-key
 * {@code max}; {@link Stemma} states what that reduces away.</p>
 */
public class MultiMutableStemma {

    private final Map<String, MutableStemma> stemmas = new LinkedHashMap<>();

    /** An accumulator seeded from {@code seed}, keyed by {@link Stemma#key()}. */
    public MultiMutableStemma(Map<String, MutableStemma> seed) {
        if (seed != null) {
            stemmas.putAll(seed);
        }
    }

    /** An accumulator holding just {@code one}. */
    public MultiMutableStemma(MutableStemma one) {
        if (one != null) {
            stemmas.put(one.key(), one);
        }
    }

    /**
     * A DEEP copy of {@code source}.
     *
     * <p>Deep is required, not tidiness: {@code App.applyBatch} merges into a copy and promotes it
     * only after the commit returns, precisely so a failed apply leaves the committed value
     * untouched. A shallow copy would share the {@link MutableStemma} objects and let a failed
     * window advance the position anyway — the exact defect the copy exists to prevent.</p>
     */
    public MultiMutableStemma(MultiMutableStemma source) {
        if (source != null) {
            for (Map.Entry<String, MutableStemma> e : source.stemmas.entrySet()) {
                stemmas.put(e.getKey(), new MutableStemma(e.getValue()));
            }
        }
    }

    /** The accumulators, keyed by {@link Stemma#key()}. */
    public Map<String, MutableStemma> getMutableStemmas() {
        return stemmas;
    }

    /** An immutable snapshot of every accumulator. */
    public Set<ImmutableStemma> getImmutableStemmas() {
        Set<ImmutableStemma> out = new HashSet<>();
        for (MutableStemma s : stemmas.values()) {
            out.add(s.toImmutable());
        }
        return out;
    }

    /** True when {@code key} has an accumulator. */
    public boolean containsKey(String key) {
        return stemmas.containsKey(key);
    }

    /** True when nothing has been merged in. */
    public boolean isEmpty() {
        return stemmas.isEmpty();
    }

    /** Discards every accumulator. */
    public void clear() {
        stemmas.clear();
    }

    /** The accumulator for {@code key}, or null. */
    public MutableStemma getSameMutableStemma(String key) {
        return stemmas.get(key);
    }

    /** Raises the accumulator for {@code other}'s key, creating it when this is the first. */
    public void mergeHigher(Stemma other) {
        if (other == null) {
            return;
        }
        MutableStemma target = stemmas.get(other.key());
        if (target == null) {
            target = new MutableStemma(other.getComponentID(), other.getDistributionID());
            stemmas.put(other.key(), target);
        }
        target.mergeHigher(other);
    }

    /** Raises every accumulator {@code other} carries. */
    public void mergeHigher(MultiMutableStemma other) {
        if (other != null) {
            for (MutableStemma s : other.stemmas.values()) {
                mergeHigher(s);
            }
        }
    }

    /** These accumulators rendered as a {@link Position} — one path per key. */
    public Position toPosition() {
        Collection<Path> paths = new java.util.ArrayList<>();
        for (MutableStemma s : stemmas.values()) {
            paths.add(new Path(s.getComponentID(), s.getDistributionID(),
                    s.getLowSourcePosition(), s.getHighSourcePosition()));
        }
        return new Position(paths);
    }

    /**
     * The inverse of {@link #toPosition()}.
     *
     * <p>Round-trip exactness is what a restart case asserts: a writer reads its checkpoint row,
     * rebuilds through here, and reports the position back out of {@code durablePosition()}. If
     * the two directions disagreed, a recovery case would fail on this mock rather than on the
     * writer.</p>
     */
    public static MultiMutableStemma fromPosition(Position position) {
        MultiMutableStemma out = new MultiMutableStemma(new LinkedHashMap<>());
        if (position != null) {
            for (Path path : position.values()) {
                MutableStemma s = new MutableStemma(path.getComponentID(), path.getDistributionID());
                s.mergeHigher(new ImmutableStemma(path.getComponentID(), path.getDistributionID(),
                        path.getLowSourcePosition(), path.getHighSourcePosition()));
                out.stemmas.put(s.key(), s);
            }
        }
        return out;
    }

    @Override
    public String toString() {
        return "MultiMutableStemma" + stemmas.values();
    }
}
