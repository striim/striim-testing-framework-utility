package com.webaction.runtime;

import java.util.ArrayList;
import java.util.List;

import com.webaction.event.Event;
import com.webaction.runtime.components.FlowComponent;

/**
 * Mock of the platform's {@code com.webaction.runtime.NotifyExceptionStore}: the route a
 * component pushes undelivered events to the application's exception store by. The real class
 * checks the application's {@code exceptionstoreName}, drops the events silently when there is
 * none, and otherwise publishes an {@code ExceptionEvent} to the store's stream.
 *
 * <p>This one RECORDS. A tier case that skips rows under {@code IgnorableExceptionCode} asserts
 * what reached the store through {@code TargetReport.exceptionStore}, which
 * {@code TargetCore} fills from {@link #drain()} after the run. Only the overload operators
 * call is declared, matching the platform signature.</p>
 */
public class NotifyExceptionStore {

    /** One recorded notification. */
    public static final class Notification {
        public final String reason;
        public final String cause;
        public final List<Event> events;

        Notification(final String reason, final String cause, final List<Event> events) {
            this.reason = reason;
            this.cause = cause;
            this.events = events;
        }
    }

    private static final NotifyExceptionStore INSTANCE = new NotifyExceptionStore();
    private static final List<Notification> RECORDED = new ArrayList<>();

    public static NotifyExceptionStore getInstance() {
        return INSTANCE;
    }

    public void notify(final FlowComponent component, final Exception cause, final long when,
                       final String reason, final Event[] events) {
        if (component == null) {
            // The real class dereferences the component at once; a null NPEs there. Same here,
            // so a caller that forgot the handle fails in the tier as it would in production.
            throw new NullPointerException("flow component is required");
        }
        synchronized (RECORDED) {
            RECORDED.add(new Notification(reason, String.valueOf(cause),
                    new ArrayList<>(java.util.Arrays.asList(events))));
        }
    }

    /** Everything recorded since the last drain, oldest first. */
    public static List<Notification> drain() {
        synchronized (RECORDED) {
            final List<Notification> out = new ArrayList<>(RECORDED);
            RECORDED.clear();
            return out;
        }
    }
}
