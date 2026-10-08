package com.striim.testing.inttest;

import java.util.List;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * The one-event-in, zero-or-more-events-out contract both drivers satisfy: {@link
 * OperatorCore} (an OpenProcessor's {@code Processor.processEvent}) and {@link UdfCore}
 * (a bare UDF pipeline). {@link IntegrationProcessor#drive} and {@link
 * PerformanceProcessor}'s measured/warmup loops are written against this interface so
 * neither knows which concrete driver it is replaying.
 */
interface EventDriver {

    /**
     * Feeds {@code event} to the driven core and returns the emitted events (empty,
     * never {@code null}, when nothing is emitted).
     *
     * <p>The INPUT is {@link Event}, not {@code WAEvent}: an operator that converts a wire format
     * is fed the format's own event type — AvroConverterOp takes an {@code AvroEvent}, which
     * extends {@code Event} and is not a {@code WAEvent}. The OUTPUT stays {@code WAEvent},
     * because what a driven core emits into a Striim stream always is one.</p>
     */
    List<WAEvent> processEvent(Event event) throws Exception;

    /**
     * Signals that a measured or warmup window is complete.
     *
     * <p>A no-op for a processor or UDF, whose work is finished the moment {@code processEvent}
     * returns. <b>A TARGET is different</b>: {@code accept()} only accumulates, and the write —
     * the round trips, the transaction, the checkpoint — happens in {@code flush()}. Measuring a
     * writer without this would time the fold and nothing about the database (§64.3).</p>
     *
     * <p>Called ONCE per window rather than per replay, and <b>inside the timed region</b>, so the
     * writer's own {@code BatchPolicy} governs how often it actually commits — which is what it
     * does in a real deployment. Flushing per replay would impose a transaction per fixture pass
     * and measure a cadence no flow would ever run.</p>
     */
    default void endOfWindow() throws Exception {
    }

    /** Releases the driven core's resources, if it declared any; a no-op otherwise. */
    void close() throws Exception;

    /**
     * Narrows an input event for a driver that only ever handles {@code WAEvent}s.
     *
     * <p>A UDF pipeline and a Target are both fed Striim stream events; only an OP core can be
     * fed something else. The message names the driver and the fixture kind that would fix it,
     * because the mistake this catches is an {@code avro} fixture pointed at the wrong block —
     * and a raw {@code ClassCastException} would read as an operator defect.</p>
     */
    static WAEvent requireWAEvent(Event event, String driver) {
        if (event instanceof WAEvent waEvent) {
            return waEvent;
        }
        throw new IllegalArgumentException(driver + " is fed WAEvents, but the fixture supplied a "
                + (event == null ? "null" : event.getClass().getSimpleName())
                + ". Only an `op:` case's Processor can take another event kind; check the"
                + " fixture's `kind`.");
    }
}
