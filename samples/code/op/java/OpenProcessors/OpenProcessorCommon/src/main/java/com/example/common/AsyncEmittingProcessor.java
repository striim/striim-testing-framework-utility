package com.example.common;

/**
 * Implemented by a core that emits <b>outside</b> {@code processEvent} — a retry sweep, a timed
 * flush, a background drain.
 *
 * <p><b>Optional and opt-in.</b> {@link AbstractOpenProcessorApp} checks for this interface after
 * {@code buildProcessor} returns and injects an emitter only if the core implements it; a core that
 * emits solely from {@code processEvent} ignores this type entirely and is unaffected.</p>
 *
 * <p>Implementing it replaces the per-op wiring this pattern used to require — a callback setter on
 * the core, a lambda in the {@code App} that looped, logged, and caught the emit's checked
 * exception, and a nullable callback field the core had to null-guard before every use. Each of
 * those was a place to get it wrong once per module: an empty {@code catch} silently drops a retry
 * result. The base supplies a non-null emitter before {@code start()} runs, so a core need not
 * defend against a missing one in production — but a core built outside an app (a unit test, the
 * integration harness) still gets none, so keep the null branch and make it say so.</p>
 *
 * <p>The injected emitter attaches the position of the last event the app processed — see
 * {@code AbstractOpenProcessorApp.emitAsync} for why that approximation is the safe one.</p>
 */
public interface AsyncEmittingProcessor {

    /**
     * Receives the emitter to use for out-of-band emits. Called once, after the core is built and
     * before {@code start()}, so a background thread started by {@code start()} always sees a
     * non-null emitter.
     */
    void setAsyncEmitter(AsyncEmitter emitter);
}
