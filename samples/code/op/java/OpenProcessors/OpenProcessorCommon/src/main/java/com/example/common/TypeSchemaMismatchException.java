package com.example.common;

/**
 * A type of the wanted name already exists in the MDR with a <b>different shape</b>, and the caller
 * asked to be told rather than to overwrite it.
 *
 * <p><b>Why this is an exception and not a {@code null} return.</b> Every caller of
 * {@link TypeResolver#createType} already treats {@code null} as "creation failed", and a collision
 * is not that. A failed creation is transient and worth retrying; a collision is deterministic and
 * permanent — the same two schemas will disagree on every subsequent event, forever. Collapsing the
 * two into one signal makes a caller's retry logic wrong in whichever direction it guessed. A
 * caller that, on a {@code null}, abandons registration with the log line
 * <i>"it may have been created"</i> is simply wrong about a collision.
 *
 * <p>Extends {@link IllegalStateException} so a caller that only wants the existing broad
 * {@code catch} still behaves sanely; catch this type specifically to say something better.
 */
public class TypeSchemaMismatchException extends IllegalStateException {

    private static final long serialVersionUID = 1L;

    private final String typeName;
    private final String mismatch;

    public TypeSchemaMismatchException(String typeName, String mismatch, String detail) {
        super("TYPE NAME COLLISION on '" + typeName + "' — " + mismatch
                + ". REFUSING to adopt or overwrite the existing type: event data[i] is positional "
                + "against field i, so reusing a type of a different shape would write this app's "
                + "values under the other type's column names (silent data corruption), and "
                + "replacing it would clobber whatever else already depends on it. " + detail);
        this.typeName = typeName;
        this.mismatch = mismatch;
    }

    /** The fully-qualified type name that collided. */
    public String typeName() {
        return typeName;
    }

    /** The FIRST facet on which the two schemas disagree, rendered for a human. */
    public String mismatch() {
        return mismatch;
    }
}
