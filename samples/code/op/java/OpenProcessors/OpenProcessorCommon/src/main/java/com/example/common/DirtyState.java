package com.example.common;

/**
 * What {@link ComponentStateCheckpointer} asks before writing. A state POJO implements this;
 * no reflection is used to decide whether a persist is needed.
 */
public interface DirtyState {

    /** True when the state changed since the last successful persist. */
    boolean isDirty();

    /** Clears the dirty flag; called only after a successful persist. */
    void markClean();
}
