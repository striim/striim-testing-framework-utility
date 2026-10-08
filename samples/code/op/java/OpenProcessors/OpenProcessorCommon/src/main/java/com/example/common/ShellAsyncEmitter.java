package com.example.common;

import java.util.List;

import com.webaction.proc.events.WAEvent;

/**
 * The {@link AsyncEmitter} the shell injects into an {@link AsyncEmittingProcessor}: both
 * {@link #emit} and {@link #emitAll} go through the app's own send with the last completed
 * position attached, and a failed send is logged rather than thrown, per the interface contract.
 *
 * <p>{@link #emitAll} is the reason this is a class rather than the {@code this::emitAsyncLogged}
 * method reference it replaced: a lambda can only ever get the interface's one-at-a-time default,
 * and a group released from a background thread — a reassembled transaction, a retry sweep's
 * results — needs the same all-or-nothing send the batch loop uses (design §5.2).</p>
 *
 * <p>A top-level class, per the OP classloader rule: an inner class in an OP jar does not load.</p>
 */
public final class ShellAsyncEmitter implements AsyncEmitter {

    private final AbstractConvertingOpenProcessorApp<?> app;

    ShellAsyncEmitter(AbstractConvertingOpenProcessorApp<?> app) {
        this.app = app;
    }

    @Override
    public void emit(WAEvent event) {
        app.emitAsyncLogged(event);
    }

    @Override
    public void emitAll(List<? extends WAEvent> events) {
        app.emitAllAsyncLogged(events);
    }
}
