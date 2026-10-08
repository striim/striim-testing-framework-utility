package com.webaction.recovery;

/**
 * Mock of the platform's {@code com.webaction.recovery.Acknowledgeable}.
 *
 * <p><b>Empty in the platform too, and that is the point.</b> {@code Target} keys its entire
 * delivery model on {@code adapter instanceof Acknowledgeable}: the flag it sets gates both the
 * pinning of received positions and the injection of {@code receiptCallback}. A writer that omits
 * the marker is never called back and silently loses whatever it had not committed — with no
 * compiler help, because the interface declares no methods.</p>
 *
 * <p>{@code TargetCore} reproduces that gate rather than always injecting a callback, so a field
 * writer that drops the marker fails in this tier the way it would in production.</p>
 */
public interface Acknowledgeable {
}
