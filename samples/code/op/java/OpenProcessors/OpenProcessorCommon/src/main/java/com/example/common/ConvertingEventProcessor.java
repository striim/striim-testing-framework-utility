package com.example.common;

import java.util.List;

/**
 * The core contract for an OP whose input and output event kinds DIFFER — a format converter.
 * {@link EventProcessor} is the same-kind specialisation and is what most OPs implement.
 *
 * <p><b>Why this exists.</b> {@code EventProcessor<E>} declares {@code List<E> processEvent(E)},
 * which cannot express a converter that consumes {@code proc.events.AvroEvent} and
 * produces {@code proc.events.WAEvent}. Before this interface, such a core could only be driven by
 * a hand-rolled {@code App} that duplicated the shared drain loop — which is precisely the
 * duplication {@link AbstractConvertingOpenProcessorApp} exists to remove.</p>
 *
 * <p><b>Nothing that implements {@code EventProcessor} changes.</b> That interface now extends this
 * one with {@code I} and {@code O} bound to the same type, so every existing core satisfies it
 * unchanged and every existing {@code App} keeps compiling — the covariant return on
 * {@code buildProcessor} does the rest.</p>
 *
 * <p>An implementing {@code Processor} provides exactly ONE constructor, in one of the Gold
 * Standard shapes — see {@link EventProcessor} for that list, which applies identically here. The
 * integration harness injects by parameter TYPE and enforces the count.</p>
 *
 * @param <I> the event kind this processor consumes
 * @param <O> the event kind it produces
 */
public interface ConvertingEventProcessor<I extends com.webaction.event.Event,
        O extends com.webaction.event.Event> extends AutoCloseable {

    /** Optional post-construction startup (background preload, metrics, …). Default: no-op. */
    default void start() throws Exception {
    }

    /** Converts one inbound event into 0..N outbound events, in emit order. */
    List<O> processEvent(I event);

    /** Releases any held resources (DB connections, etc). Never throws. */
    @Override
    void close();
}
