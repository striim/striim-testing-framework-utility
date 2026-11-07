package com.webaction.recovery;

import java.util.List;

import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.recovery.ImmutableStemma} — the position a Target
 * is HANDED, on {@code processEvent(int, Event, ImmutableStemma)}.
 *
 * <p>Concrete here; abstract in the platform, where the concrete shape depends on how many parents
 * the node has. {@link Stemma}'s javadoc states why this mock has no parents at all.</p>
 */
public class ImmutableStemma extends Stemma {

    private static final long serialVersionUID = 1L;

    private final SourcePosition low;
    private final SourcePosition high;
    /**
     * {@link #toPaths}, built once: the platform's {@code InitialImmutableStemma} caches its paths,
     * so a repeated call returns the same list there and must cost nothing here either.
     */
    private transient List<Path> cachedPaths;

    /** A position at {@code low}, with no separate high water mark. */
    public ImmutableStemma(UUID componentID, String distributionID, SourcePosition low) {
        this(componentID, distributionID, low, low);
    }

    /** A position spanning {@code low}..{@code high}. */
    public ImmutableStemma(UUID componentID, String distributionID, SourcePosition low,
            SourcePosition high) {
        super(componentID, distributionID);
        this.low = low;
        this.high = high;
    }

    @Override
    public SourcePosition getLowSourcePosition() {
        return low;
    }

    @Override
    public SourcePosition getHighSourcePosition() {
        return high;
    }

    @Override
    public List<Path> toPaths() {
        List<Path> paths = cachedPaths;
        if (paths == null) {
            paths = super.toPaths();
            cachedPaths = paths;
        }
        return paths;
    }

    /** The single stemma a one-path {@link Position} describes, or null when it has none. */
    public static ImmutableStemma oneImmutableStemmaFromPosition(Position position) {
        if (position == null || position.isEmpty()) {
            return null;
        }
        Path path = position.values().iterator().next();
        return new ImmutableStemma(path.getComponentID(), path.getDistributionID(),
                path.getLowSourcePosition(), path.getHighSourcePosition());
    }
}
