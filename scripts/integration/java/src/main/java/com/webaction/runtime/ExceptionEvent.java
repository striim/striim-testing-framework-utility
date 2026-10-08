package com.webaction.runtime;

import org.joda.time.DateTime;

import com.webaction.event.Event;

/**
 * Mock of the platform's {@code com.webaction.runtime.ExceptionEvent} — the native event type an
 * exception-store reader emits under {@code outputType=EXCEPTIONEVENT}.
 *
 * <p><b>Why the harness needs it even for a case that never emits one.</b> A core that *can* build
 * a native exception event references this class from a method body, and the JVM resolves that
 * reference when the referring class is verified — at construction time, long before any tick
 * chooses an output mode. Without this class an exception-store reader cannot be constructed by the
 * harness at all, which is what blocked an exception-store reader's tier.</p>
 *
 * <p><b>The field types are the contract, not a convenience.</b> A field reference in the op jar's
 * bytecode carries name AND descriptor, so these must match the real class exactly — in particular
 * {@code exceptionTime} is a joda {@link DateTime}, not a {@code Long} of epoch millis. A mock that
 * simplified it would resolve to {@code NoSuchFieldError} the first time a core assigned it.</p>
 *
 * <p>The real class extends {@code SimpleEvent}; this extends {@link Event} directly, which is the
 * same thing from an op's perspective — {@code SimpleEvent} adds no member an op assigns.</p>
 */
public class ExceptionEvent extends Event {

    public String exceptionType;
    public String action;
    public String appName;
    public String appid;
    public String entityType;
    public String entityName;
    public String className;
    public String message;
    public DateTime exceptionTime;
    public Long epochNumber;
    public String relatedActivity;
    public String relatedObjects;
    public String relatedEntity;
    public String exceptionCode;

    /**
     * <b>Deliberate fidelity gap:</b> the real no-arg constructor seeds {@code exceptionTime} to
     * {@code now()} and {@code epochNumber} to {@code -1}; this one leaves both null.
     *
     * <p>Seeding a wall-clock default into a harness mock would make any case that emitted a native
     * event without overwriting it <b>nondeterministic</b> — the tier's cardinal sin — where a null
     * fails cleanly and visibly instead. Cores that build these overwrite both fields
     * unconditionally, so nothing depends on the seed; a future core that does would be relying on
     * a default it should be setting.</p>
     */
    public ExceptionEvent() {
    }
}
