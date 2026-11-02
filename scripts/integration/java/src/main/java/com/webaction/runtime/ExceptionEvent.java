package com.webaction.runtime;

import java.util.Arrays;

import org.joda.time.DateTime;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.event.Event;
import com.webaction.exceptionhandling.ExceptionType;
import com.webaction.runtime.components.EntityType;
import com.webaction.uuid.UUID;

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

    /**
     * The real 13-argument constructor, which {@code NotifyExceptionStore.notify} uses and field's
     * {@code ExceptionStorePublisher} now calls itself. Field for field as the real one (cfr,
     * 5.4.0.6G): the type, action and entity type are stored as their names, the application's
     * uuid as its string (a null uuid throws, as it does there), and the related events as a
     * string of their JSON.
     *
     * <p>Two differences, both deliberate. {@code exceptionTime} is left null where the real
     * constructor stamps {@code now()}, for the reason the no-arg constructor gives. And the events
     * themselves are kept, beside their JSON, in a field only this package reads:
     * {@code NotifyExceptionStore} records them, by identity, so a case can name which input
     * events a skipped row carried -- the real event keeps only the string.</p>
     */
    public ExceptionEvent(ExceptionType exceptionType, ActionType actionType, String appName,
            UUID appUUID, String entityName, EntityType entityType, String className,
            String message, Long epochNumber, String relatedActivity, Event[] relatedObjects,
            String relatedEntity, String exceptionCode) {
        this.exceptionType = exceptionType.toString();
        if (actionType != null) {
            this.action = actionType.toString();
        }
        this.appName = appName;
        this.appid = appUUID.toString();
        this.entityName = entityName;
        if (entityType != null) {
            this.entityType = entityType.toString();
        }
        this.className = className;
        this.message = message;
        this.epochNumber = epochNumber;
        this.relatedActivity = relatedActivity;
        if (relatedObjects != null) {
            String[] json = new String[relatedObjects.length];
            for (int i = 0; i < relatedObjects.length; i++) {
                json[i] = toJSON(relatedObjects[i]);
            }
            this.relatedObjects = Arrays.toString(json);
            this.relatedEvents = relatedObjects.clone();
        }
        this.relatedEntity = relatedEntity;
        this.exceptionCode = exceptionCode;
    }

    /** Harness-only: the events {@link #relatedObjects} was made from, as the caller passed them. */
    transient Event[] relatedEvents;

    private static final ObjectMapper JSON = new ObjectMapper();

    /** The real method, over a plain Jackson mapper where the platform uses its own. */
    public String toJSON(Object object) {
        try {
            return JSON.writeValueAsString(object);
        } catch (Exception exception) {
            return "Cannot map object, " + String.valueOf(object) + "to JSON.";
        }
    }
}
