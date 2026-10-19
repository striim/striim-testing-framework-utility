package com.webaction.runtime.lee;

/**
 * Mock of the platform's {@code com.webaction.runtime.lee.BatchAcknowledgeable}.
 *
 * <p>An empty marker that extends {@link com.webaction.recovery.Acknowledgeable}, as in the
 * platform. A target that implements it is still acknowledgeable, so {@code TargetCore}'s
 * {@code instanceof Acknowledgeable} gate applies to it unchanged. In the platform the marker also
 * tells {@code TargetLeeHelper} not to keep a per-event end-to-end latency entry for a target that
 * acks by batch; the harness has no latency map, so nothing here reads it.</p>
 *
 * <p>It exists so an operator jar whose adapter class implements the interface can be loaded:
 * without it the harness fails at {@code Class.forName} with {@code NoClassDefFoundError}.</p>
 */
public interface BatchAcknowledgeable extends com.webaction.recovery.Acknowledgeable {
}
