package com.example.common;

import com.webaction.event.Event;

/**
 * The 0..N output seam for cores that emit events not 1:1 with input — readers and drain loops.
 * Production implementations call the framework {@code send(...)}; a test harness injects a
 * recording fake.
 *
 * <p><b>Generic over the element type, and that is the whole point.</b> A flat
 * {@code emit(Event)} could not be implemented by a reader whose seam is {@code emit(WAEvent)}:
 * <b>Java does not permit narrowing a parameter type in an override</b>, so {@code emit(WAEvent)}
 * would <i>overload</i> rather than override, leaving an implementer obliged to provide both.</p>
 *
 * <p>A reader reaches this seam in one of two ways: it implements {@code Emitter<Event>} or
 * {@code Emitter<WAEvent>} directly, or it extends {@link AbstractSourceApp}, which implements
 * {@link SourceChannel} ({@code emit} plus {@code checkpoint(P)}) on its behalf. A reader that
 * emits on a channel other than 0 keeps the channel in a field on its {@code App}, assigned from
 * {@code receiveImpl}'s argument and read back at its {@code send} site; the core carries no
 * channel.</p>
 */
public interface Emitter<E extends Event> {

    void emit(E event) throws Exception;
}
