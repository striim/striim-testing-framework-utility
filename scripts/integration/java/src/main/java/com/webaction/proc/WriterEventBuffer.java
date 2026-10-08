package com.webaction.proc;

import java.util.ArrayList;
import java.util.List;

import com.webaction.event.Event;

/**
 * Mock of the platform's {@code com.webaction.proc.WriterEventBuffer} — the in-flight window
 * {@code RetriableWriter} replays on reconnect.
 *
 * <p>{@code AbstractWriterApp.ackBatch} calls {@link #clear(Event)} with the newest acked event,
 * relying on the real buffer's monotonic contract: clearing at an index discards everything at or
 * below it. That contract is reproduced here so a tier case can see the window shrink.</p>
 *
 * <p><b>Never populated by this tier.</b> The platform fills the buffer inside {@code receive},
 * above the seam {@code TargetCore} drives; {@code TargetCore} leaves {@code eventBuffer} null,
 * which is exactly what {@code ackBatch} null-guards for. The class exists so the field's type
 * resolves and so a future case that DOES populate it has the contract to test against.</p>
 */
public class WriterEventBuffer {

    private final List<Event> events = new ArrayList<>();

    /** How many events are still in flight. */
    public synchronized int size() {
        return events.size();
    }

    /** Records an event as in flight. */
    public synchronized void add(Event event) {
        events.add(event);
    }

    /** Discards the whole window. */
    public synchronized void clear() {
        events.clear();
    }

    /** Discards everything at or below {@code index}. */
    public synchronized void clear(int index) {
        for (int i = Math.min(index, events.size() - 1); i >= 0; i--) {
            events.remove(i);
        }
    }

    /** Discards everything at or below {@code event}; a no-op when it is not held. */
    public synchronized void clear(Event event) {
        int index = events.indexOf(event);
        if (index >= 0) {
            clear(index);
        }
    }

    /** Everything still in flight, oldest first. */
    public synchronized List<Event> getEvents() {
        return new ArrayList<>(events);
    }
}
