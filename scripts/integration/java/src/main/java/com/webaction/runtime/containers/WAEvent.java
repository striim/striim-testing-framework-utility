package com.webaction.runtime.containers;

import java.io.Serializable;

import com.webaction.recovery.ImmutableStemma;

/**
 * Mock of the platform's {@code com.webaction.runtime.containers.WAEvent}: the envelope a task
 * event carries a payload in. Not {@code com.webaction.proc.events.WAEvent}, the CDC event a
 * writer receives; the two share a simple name and nothing else.
 *
 * <p>{@code data} and {@code position} are the real public fields, and the three constructors are
 * the real ones. The lag-marker and LEE fields and the Kryo serialisation are omitted: nothing in
 * this tier sets or serialises them.</p>
 */
public class WAEvent implements Serializable {

    private static final long serialVersionUID = 1L;

    /** The payload: an {@code ExceptionEvent} on the exception stream. */
    public Object data;
    /** The payload's recovery position, or null. */
    public ImmutableStemma position;

    public WAEvent() {
    }

    public WAEvent(final Object data) {
        this.data = data;
    }

    public WAEvent(final Object data, final ImmutableStemma position) {
        this.data = data;
        this.position = position;
    }

    @Override
    public String toString() {
        return "WAEvent(" + data + ")";
    }
}
