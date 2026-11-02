package com.webaction.runtime;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

import com.webaction.event.Event;
import com.webaction.runtime.channels.Channel;
import com.webaction.runtime.components.FlowComponent;
import com.webaction.runtime.components.Stream;
import com.webaction.runtime.containers.ITaskEvent;
import com.webaction.runtime.containers.WAEvent;

/**
 * Mock of the platform's {@code com.webaction.runtime.NotifyExceptionStore}: the route a
 * component pushes undelivered events to the application's exception store by. The real class
 * checks the application's {@code exceptionstoreName}, drops the events silently when there is
 * none, and otherwise publishes an {@code ExceptionEvent} to the store's stream.
 *
 * <p>This one RECORDS. A tier case that skips rows under {@code IgnorableExceptionCode} asserts
 * what reached the store through {@code TargetReport.exceptionStore}, which
 * {@code TargetCore} fills from {@link #drain()} after the run.</p>
 *
 * <p><b>Two routes in, one record.</b> A component may call {@link #notify} (the platform's
 * own entry, which this mock records directly), or do what field's {@code ExceptionStorePublisher}
 * does since OpenProcessorCommon I9: build the {@link ExceptionEvent} itself and publish it on
 * {@link #getExceptionStream()}'s channel, so it can tell a missing route from a delivered row.
 * Publishing is what the real {@link #publishException} does too. The stand-in exception stream's
 * channel records every {@code ExceptionEvent} published on it as a {@link Notification}: the
 * reason is the event's {@code relatedActivity}, the cause is {@code "<className>: <message>"}
 * (what {@code String.valueOf(exception)} gave the direct route), and the events are the ones
 * passed to the event's constructor, by identity.</p>
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

    /**
     * The server's exception stream. Always present here: on a server it exists whenever an
     * application can use a store, and whether THIS application declared one is read from the
     * application's metadata, not from the stream.
     */
    private static final Stream EXCEPTION_STREAM = new Stream(new RecordingChannel());

    public static NotifyExceptionStore getInstance() {
        return INSTANCE;
    }

    /** The server's exception stream; never null here. */
    public Stream getExceptionStream() throws Exception {
        return EXCEPTION_STREAM;
    }

    /**
     * The overload operators call, matching the platform signature. Records directly, without
     * consulting the application's metadata: the real method's store check reads the MDR, which
     * this tier lacks.
     */
    public void notify(final FlowComponent component, final Exception cause, final long when,
                       final String reason, final Event[] events) {
        if (component == null) {
            // The real class dereferences the component at once; a null NPEs there. Same here,
            // so a caller that forgot the handle fails in the tier as it would in production.
            throw new NullPointerException("flow component is required");
        }
        record(new Notification(reason, String.valueOf(cause),
                new ArrayList<>(Arrays.asList(events))));
    }

    /**
     * Publishes {@code event} on the exception stream's channel under this object's monitor, as
     * the real method does; here that records it. The real method logs and swallows a failure to
     * publish; nothing here can fail.
     */
    public void publishException(final ExceptionEvent event) {
        synchronized (this) {
            try {
                final Channel channel = getExceptionStream().getChannel();
                final List<WAEvent> batch = new ArrayList<>(1);
                batch.add(new WAEvent(event));
                channel.publish(com.webaction.runtime.containers.TaskEvent.createStreamEvent(batch));
            } catch (Exception e) {
                throw new IllegalStateException("the stand-in exception stream refused an event", e);
            }
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

    private static void record(final Notification notification) {
        synchronized (RECORDED) {
            RECORDED.add(notification);
        }
    }

    /**
     * The exception stream's channel: records each {@link ExceptionEvent} in a published batch.
     * Anything else on it is refused, so a publisher that wrapped the wrong payload fails loudly
     * in the tier instead of recording nothing.
     */
    private static final class RecordingChannel implements Channel {
        @Override
        public void publish(final ITaskEvent event) {
            for (WAEvent envelope : event.batch()) {
                if (!(envelope.data instanceof ExceptionEvent)) {
                    throw new IllegalArgumentException("the exception stream carries ExceptionEvents;"
                            + " got " + (envelope.data == null ? "null" : envelope.data.getClass().getName()));
                }
                final ExceptionEvent ee = (ExceptionEvent) envelope.data;
                record(new Notification(ee.relatedActivity,
                        ee.message == null ? ee.className : ee.className + ": " + ee.message,
                        ee.relatedEvents == null ? new ArrayList<>()
                                : new ArrayList<>(Arrays.asList(ee.relatedEvents))));
            }
        }
    }
}
