package com.webaction.recovery;

import java.io.Serializable;

import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.recovery.Stemma}, the root of the recovery
 * family a Target's positions travel in.
 *
 * <h2>The reduction, stated plainly</h2>
 * The real {@code Stemma} is a NODE IN A LATTICE: it carries a collection of parents, and a
 * merge is a lattice join over that whole ancestry. <b>This mock is one level deep.</b> A
 * stemma here is a (component, distribution) key plus a low and a high {@link SourcePosition},
 * with no parents at all, and a merge is a per-key {@code max}.
 *
 * <p>That is enough for the integration tier and no more. A tier case drives ONE writer from ONE
 * scripted position stream, so every stemma it ever sees is a single path — the shape where a
 * lattice join and a per-key max agree. A branching topology (a router fanning one source into
 * two writers, then a join) is where they diverge, and that shape needs a real Striim: it is T4's,
 * not this tier's. This tier tests
 * the writer against OUR MODEL of the lifecycle; this class is the largest single piece of that
 * model, so the divergence is named here rather than left to be discovered.</p>
 *
 * <p>Shape matches the platform class's public surface on 5.4. The members
 * the platform declares and nothing in {@code AbstractWriterApp} or a sample writer calls are
 * omitted rather than stubbed to throw — an absent method is a link error naming itself, and a
 * throwing one is a runtime surprise inside whatever called it.</p>
 */
public abstract class Stemma implements Serializable {

    private static final long serialVersionUID = 1L;

    /** {@code AT} semantics: the position IS the last thing processed. */
    public static final String AT = "AT";
    /** {@code AFTER} semantics: processing resumes past the position. */
    public static final String AFTER = "AFTER";

    private final UUID componentID;
    private final String distributionID;

    /** A stemma with no identity, for a subclass that fills one in later. */
    protected Stemma() {
        this(null, null);
    }

    /** A stemma identified by the component that produced it and its distribution key. */
    protected Stemma(UUID componentID, String distributionID) {
        this.componentID = componentID;
        this.distributionID = distributionID;
    }

    /** The component this position came from. */
    public UUID getComponentID() {
        return componentID;
    }

    /** The distribution key within that component. */
    public String getDistributionID() {
        return distributionID;
    }

    /**
     * The identity a merge groups on: component and distribution together.
     *
     * <p>Null-tolerant on both halves, because a scripted tier position may name only one.</p>
     */
    public String key() {
        return (componentID == null ? "-" : componentID.toString())
                + "/" + (distributionID == null ? "-" : distributionID);
    }

    /** True when {@code other} carries the same {@link #key()}. */
    public boolean isSameComponent(Stemma other) {
        return other != null && key().equals(other.key());
    }

    /** The low water mark — everything at or below it is accounted for. */
    public abstract SourcePosition getLowSourcePosition();

    /** The high water mark. */
    public abstract SourcePosition getHighSourcePosition();

    /** The high mark when there is one, else the low. */
    public SourcePosition getHighOrLowSourcePosition() {
        return getHighSourcePosition() != null ? getHighSourcePosition() : getLowSourcePosition();
    }

    /** This stemma rendered as a one-path {@link Position}. */
    public Position toPosition() {
        return new Position(new Path(getComponentID(), getDistributionID(),
                getLowSourcePosition(), getHighSourcePosition()));
    }

    @Override
    public String toString() {
        return getClass().getSimpleName() + "(" + key() + ", low=" + getLowSourcePosition()
                + ", high=" + getHighSourcePosition() + ")";
    }
}
