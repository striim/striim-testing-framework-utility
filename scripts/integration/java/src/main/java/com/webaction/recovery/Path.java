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
