package com.example.common;

import com.webaction.proc.events.WAEvent;

/**
 * Emits an event from outside the batch loop, on whatever thread the core is running.
 *
 * <p><b>Deliberately declares no checked exception.</b> The implementation the framework hands a
 * core ({@link AbstractOpenProcessorApp}'s wiring) already catches and logs a failed emit, so a core
 * calls {@code emitter.emit(event)} and moves on to the next one. A {@code throws Exception} here
 * would push that burden onto every background thread that emits — and the shortcut an author
 * reaches for under a checked exception is an empty {@code catch}, which turns a failed emit into
 * silent data loss. One correct handler in the base beats N hand-written ones.</p>
 *
 * <p>Consequence a core should know: {@code emit} is best-effort and reports nothing back. A core
 * needing to distinguish emitted-from-failed must not use this seam.</p>
 */
@FunctionalInterface
public interface AsyncEmitter {

    /**
     * Emits one event, carrying the last position the app processed.
     *
     * <p>Implementations MUST NOT throw — a core calls this from a background thread with no
     * handler of its own. The framework's implementation catches {@code Exception} and logs; note
     * that an {@code Error} still escapes, as it should.</p>
     */
    void emit(WAEvent event);

    /**
     * Emits a GROUP — everything one release produced, a reassembled transaction say — as one
     * unit, every member carrying the last position the app processed.
     *
     * <p>The framework's implementation ({@link ShellAsyncEmitter}) sends the group as ONE
     * {@code TaskEvent}, so it is all-or-nothing at the emission boundary (design
     * §5.2): a failure cannot leave the first half of a transaction downstream and lose the rest.
     * <b>This default is NOT that</b> — it emits one at a time, and exists so a test's lambda
     * (`emitted::add`) still satisfies the interface. A core that emits a group should call this,
     * not loop over {@link #emit}; a lint can flag the loop.</p>
     */
    default void emitAll(java.util.List<? extends WAEvent> events) {
        if (events == null) {
            return;
        }
        for (WAEvent e : events) {
            emit(e);
        }
    }
}
