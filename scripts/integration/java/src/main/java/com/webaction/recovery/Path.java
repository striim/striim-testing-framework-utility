package com.webaction.recovery;

import java.io.Serializable;
import java.util.Objects;

import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.recovery.Path} — one component's contribution to a
 * {@link Position}.
 *
 * <p>The real class is a LIST of path items, one per component the events traversed. This mock
 * carries a single item, for the reason {@link Stemma} gives.</p>
 */
public class Path implements Serializable, Comparable<Path> {

    private static final long serialVersionUID = 1L;

    private final UUID componentID;
    private final String distributionID;
    private final SourcePosition low;
    private final SourcePosition high;
    /**
     * {@link #getPathHash}, computed on first use: the real class interns its item list and caches
     * the list's string, so a repeated call costs nothing there and must cost nothing here, or the
     * perf tier prices a call count the platform does not pay. 0 until computed.
     */
    private transient int pathHash;
    private transient boolean pathHashed;

    /** A path at {@code low}, with no separate high water mark. */
    public Path(UUID componentID, String distributionID, SourcePosition low) {
        this(componentID, distributionID, low, low);
    }

    /** A path spanning {@code low}..{@code high}. */
    public Path(UUID componentID, String distributionID, SourcePosition low, SourcePosition high) {
        this.componentID = componentID;
        this.distributionID = distributionID;
        this.low = low;
        this.high = high;
    }

    /** The component this path segment names. */
    public UUID getComponentID() {
        return componentID;
    }

    /** The distribution key within that component. */
    public String getDistributionID() {
        return distributionID;
    }

    /** The low water mark. {@code AbstractWriterApp} renders exactly this for monitoring. */
    public SourcePosition getLowSourcePosition() {
        return low;
    }

    /** The high water mark. */
    public SourcePosition getHighSourcePosition() {
        return high;
    }

    /**
     * The hash of this path's item list, which the platform's duplicate filter and
     * {@code HoldAckLedger} key a path by.
     *
     * <p>The real method is {@code pathItems.hashCode()}, and an item list hashes as its string,
     * {@code "<componentUuid>+<distributionID>"} per item joined by commas, with {@code "*"} for a
     * null uuid or a null or empty distribution ID. This mock's path has one item, so the same
     * formula over that one item gives the same answer: two paths through the same (component,
     * distribution) hash alike whatever their positions, and paths through different ones (almost
     * always) do not.</p>
     */
    public int getPathHash() {
        if (!pathHashed) {
            String component = componentID != null ? componentID.toString() : "*";
            String distribution = distributionID != null && !distributionID.isEmpty() ? distributionID : "*";
            pathHash = (component + "+" + distribution).hashCode();
            pathHashed = true;
        }
        return pathHash;
    }

    /**
     * {@code AFTER} semantics: the platform resumes past this path's position rather than at it.
     *
     * <p>Always false here. The real class carries an {@code atOrAfter} marker, {@code "@"} or
     * {@code "^"}, set by the source that made the path; this mock has no constructor that takes
     * one, so every path it builds is the real class's default, {@code AT}. A writer that compares
     * the recorded position of a path therefore compares its low, as it does for an {@code AT}
     * path on a server.</p>
     */
    public boolean isAfter() {
        return false;
    }

    /** The identity a {@link Position} keys this path under: component and distribution. */
    public String key() {
        return (componentID == null ? "-" : componentID.toString())
                + "/" + (distributionID == null ? "-" : distributionID);
    }

    @Override
    public int compareTo(Path other) {
        return key().compareTo(other.key());
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Path)) {
            return false;
        }
        Path other = (Path) o;
        return key().equals(other.key())
                && Objects.equals(low, other.low)
                && Objects.equals(high, other.high);
    }

    @Override
    public int hashCode() {
        return Objects.hash(key(), low, high);
    }

    @Override
    public String toString() {
        return "Path(" + key() + ", low=" + low + ", high=" + high + ")";
    }
}
