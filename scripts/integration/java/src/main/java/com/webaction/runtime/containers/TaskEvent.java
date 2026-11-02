package com.webaction.runtime.containers;

import java.util.Iterator;
import java.util.List;

/**
 * Mock of the platform's {@code com.webaction.runtime.containers.TaskEvent}, abstract as the real
 * class is. Only the factory field code calls is reproduced: {@link #createStreamEvent(List)},
 * which {@code ExceptionStorePublisher} wraps an {@code ExceptionEvent} in before publishing it.
 */
public abstract class TaskEvent implements ITaskEvent {

    private static final long serialVersionUID = 1L;

    public TaskEvent() {
    }

    /**
     * A task event whose {@link #batch()} is {@code added}, in order. Like the real factory it keeps
     * the caller's list rather than a copy.
     */
    public static TaskEvent createStreamEvent(final List<WAEvent> added) {
        final List<WAEvent> events = added;
        return new TaskEvent() {
            private static final long serialVersionUID = 1L;

            @Override
            public IBatch<WAEvent> batch() {
                return new ListBatch(events);
            }
        };
    }

    /** A read-only batch over a list. */
    private static final class ListBatch implements IBatch<WAEvent> {
        private static final long serialVersionUID = 1L;
        private final List<WAEvent> events;

        ListBatch(final List<WAEvent> events) {
            this.events = events;
        }

        @Override
        public Iterator<WAEvent> iterator() {
            return java.util.Collections.unmodifiableList(events).iterator();
        }

        @Override
        public int size() {
            return events.size();
        }

        @Override
        public boolean isEmpty() {
            return events.isEmpty();
        }

        @Override
        public WAEvent first() {
            return events.isEmpty() ? null : events.get(0);
        }

        @Override
        public WAEvent last() {
            return events.isEmpty() ? null : events.get(events.size() - 1);
        }
    }
}
