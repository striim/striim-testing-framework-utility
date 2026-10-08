package com.webaction.runtime.monitor;

import java.util.ArrayList;
import java.util.Collection;

/**
 * Mock of the platform's {@code com.webaction.runtime.monitor.MonitorEventsCollection} — the
 * bag a component fills during {@code publishMonitorEvents}.
 *
 * <p>Kept so a test can detect missing throughput figures when {@code publishMonitorEvents} was never overridden. A tier
 * case can now drive the hook and read back what it published.</p>
 */
public class MonitorEventsCollection {

    private final Collection<MonitorEvent> events = new ArrayList<>();
    private final long timeStamp;

    /** A collection stamped at {@code timeStamp}. */
    public MonitorEventsCollection(long timeStamp) {
        this.timeStamp = timeStamp;
    }

    /** Records one reading. */
    public <V extends Comparable> void add(MonitorEvent.Type<V> type, V value) {
        events.add(new MonitorEvent<>(type, value));
    }

    /** Records an already-built reading. */
    public void add(MonitorEvent event) {
        events.add(event);
    }

    /** Everything recorded, in the order it was published. */
    public Collection<MonitorEvent> getEvents() {
        return events;
    }

    /** When this collection was stamped. */
    public long getTimeStamp() {
        return timeStamp;
    }
}
