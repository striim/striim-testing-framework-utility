package com.webaction.runtime.monitor;

/**
 * Mock of the platform's {@code com.webaction.runtime.monitor.MonitorEvent} — one metric a
 * component publishes.
 *
 * <p>Reduced to the metric's {@link Type} and its value. The real class is an {@code Event}
 * subclass carrying serialization and aggregation machinery a tier has no monitoring pipeline to
 * consume.</p>
 *
 * @param <X> the metric's value type
 */
public class MonitorEvent<X extends Comparable> {

    /**
     * A metric's identity. The real class declares dozens; the ones below are those
     * {@code AbstractWriterApp.publishMonitorEvents} actually publishes.
     *
     * @param <V> the metric's value type
     */
    public static final class Type<V extends Comparable> {

        /** Events durably applied. The figure a monitor page shows as processed. */
        public static final Type<Long> PROCESSED = new Type<>("PROCESSED");
        /** Wall clock of the last commit. */
        public static final Type<Long> LAST_COMMIT_TIME = new Type<>("LAST_COMMIT_TIME");
        /** Wall clock of the last write to the target. */
        public static final Type<Long> LAST_IO_TIME = new Type<>("LAST_IO_TIME");
        /** How many events the last commit carried. */
        public static final Type<Long> TOTAL_EVENTS_IN_LAST_COMMIT =
                new Type<>("TOTAL_EVENTS_IN_LAST_COMMIT");
        /** How many events the last write carried. */
        public static final Type<Long> TOTAL_EVENTS_IN_LAST_IO = new Type<>("TOTAL_EVENTS_IN_LAST_IO");
        /** How long the last commit took. */
        public static final Type<Long> COMMIT_LATENCY = new Type<>("COMMIT_LATENCY");
        /** How long the last write to the target took. */
        public static final Type<Long> EXTERNAL_IO_LATENCY = new Type<>("EXTERNAL_IO_LATENCY");
        /** The durable position, rendered for display. */
        public static final Type<String> TARGET_COMMIT_POSITION = new Type<>("TARGET_COMMIT_POSITION");
        /**
         * ⚠ §155. Four more the writer publishes for §132.3. Names AND value types are the
         * platform's — this mock
         * exists to be our model of the platform (§44.1), and a Type it lacks is a
         * {@code NoSuchFieldError} at runtime against a writer that compiled fine, which is
         * exactly how these four were found.
         */
        public static final Type<Long> NUM_OF_EXCEPTIONS_IGNORED =
                new Type<>("NUM_OF_EXCEPTIONS_IGNORED");
        /** Per-operation counts, JSON. */
        public static final Type<String> OPERATION_METRICS = new Type<>("OPERATION_METRICS");
        /** Per-target-table counts, JSON. */
        public static final Type<String> TABLE_INFO = new Type<>("TABLE_INFO");
        /** Seconds since the last durable write. ⚠ Double, not Long, in the platform. */
        public static final Type<Double> LAST_WRITE_AGE = new Type<>("LAST_WRITE_AGE");
        /** §157. Rows an UPDATE or DELETE matched nothing, JSON. */
        public static final Type<String> NO_OP_OPERATIONS = new Type<>("NO_OP_OPERATIONS");
        /** Per-target-table millis behind the source's commit clock, JSON. {@code Type<String>} in the platform. */
        public static final Type<String> COMMIT_LAG = new Type<>("COMMIT_LAG");

        private final String name;

        private Type(String name) {
            this.name = name;
        }

        /** The metric's name, as the platform reports it. */
        public String getName() {
            return name;
        }

        @Override
        public String toString() {
            return name;
        }
    }

    private final Type<X> type;
    private final X value;

    /** A reading of {@code type}. */
    public MonitorEvent(Type<X> type, X value) {
        this.type = type;
        this.value = value;
    }

    /** Which metric this reading is of. */
    public Type<X> getType() {
        return type;
    }

    /** The reading. */
    public X getValue() {
        return value;
    }

    @Override
    public String toString() {
        return type + "=" + value;
    }
}
