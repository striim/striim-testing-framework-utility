package com.striim.testing.inttest;

import com.webaction.recovery.SourcePosition;

/**
 * The scripted {@link SourcePosition} a {@code target:} case's events carry: a monotonically
 * increasing ordinal, one per input event.
 *
 * <p>A real source position is opaque and vendor-shaped — an Oracle SCN, a SQL Server LSN, a
 * Spanner commit timestamp. What a writer does with it is not read it but ORDER it, so an ordinal
 * is the smallest thing that exercises the same code: merged positions take the higher, the durable
 * position rises and never falls, and a case can name the exact value it expects to see after a
 * restart. Anything more elaborate would be testing the harness's own position format.</p>
 */
public final class OrdinalPosition extends SourcePosition {

    private static final long serialVersionUID = 1L;

    private final long ordinal;

    /** A position at {@code ordinal}. */
    public OrdinalPosition(long ordinal) {
        this.ordinal = ordinal;
    }

    /** Which event in the case's input this position marks, counting from 1. */
    public long ordinal() {
        return ordinal;
    }

    @Override
    public int compareTo(SourcePosition other) {
        if (!(other instanceof OrdinalPosition)) {
            // Ordering against a foreign position is not defined, and guessing an answer would let
            // a merge silently pick one. Nothing in this tier produces a foreign position, so this
            // firing means the harness has a defect rather than the writer under test.
            throw new IllegalArgumentException("cannot order an OrdinalPosition against "
                    + (other == null ? "null" : other.getClass().getName()));
        }
        return Long.compare(ordinal, ((OrdinalPosition) other).ordinal);
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof OrdinalPosition && ((OrdinalPosition) o).ordinal == ordinal;
    }

    @Override
    public int hashCode() {
        return Long.hashCode(ordinal);
    }

    @Override
    public String toString() {
        return Long.toString(ordinal);
    }
}
