package com.example.common;

import com.webaction.event.Event;
import com.webaction.recovery.ImmutableStemma;

/**
 * One event as a writer receives it, with the position that must stay pinned until it is durable.
 *
 * <p>Immutable, and it keeps the {@link ImmutableStemma} alongside the event deliberately: the
 * position is what {@code Target.memoryCheckpoint} pins and what the ack releases, so losing track
 * of it — or acking a different one — moves the app checkpoint past data that is not on disk.</p>
 */
public final class WriteItem {

    private final int channel;
    private final Event event;
    private final ImmutableStemma position;

    public WriteItem(final int channel, final Event event, final ImmutableStemma position) {
        this.channel = channel;
        this.event = event;
        this.position = position;
    }

    /** The output channel this event arrived on. */
    public int channel() {
        return channel;
    }

    /** The event itself, unwrapped from its container. */
    public Event event() {
        return event;
    }

    /** May be null: {@code RetriableWriter} has a two-arg {@code processEvent} with no position. */
    public ImmutableStemma position() {
        return position;
    }

    @Override
    public String toString() {
        return "WriteItem{channel=" + channel
                + ", event=" + (event == null ? "null" : event.getClass().getSimpleName())
                + ", position=" + (position == null ? "null" : "present") + '}';
    }
}
