package com.example.common;

import java.util.List;

/**
 * The uniform core contract every gold OP's transform logic implements, expressed over the Striim
 * {@link com.webaction.event.Event} base so it is not locked to {@code WAEvent}.
 * {@code WAEvent}-centric OPs implement {@code EventProcessor<com.webaction.proc.events.WAEvent>};
 * standardizing on a {@code List} return removes emit-style branching in callers, since an enricher
 * just returns {@code List.of(event)}.
 *
 * <p>Forward-compat: {@code WAEvent} is the only concrete event kind exercised today, but both it
 * and {@code com.webaction.proc.events.JsonNodeEvent} extend {@code Event}, so an
 * {@code EventProcessor<JsonNodeEvent>} slots in later with no change to this contract.</p>
 *
 * <p><b>A core whose input and output kinds DIFFER implements
 * {@link ConvertingEventProcessor} instead</b> — a format converter, which
 * reads an {@code AvroEvent} and writes a {@code WAEvent}. This interface is that one with both
 * parameters bound to the same type, so nothing implementing it had to change when the converting
 * form was added.</p>
 *
 * <p>An implementing {@code Processor} provides exactly ONE constructor, in one of the Gold
 * Supported processor shapes:</p>
 * <pre>
 * Processor(Map&lt;String,Object&gt; props, Logger logger)                                  // no seam needed
 * Processor(Map&lt;String,Object&gt; props, BuiltInFuncs funcs, Logger logger)               // introspection
 * Processor(Map&lt;String,Object&gt; props, BuiltInFuncs funcs, TypeResolver types, Logger logger) // type-consuming
 * </pre>
 * {@code Logger} is mandatory in every shape; {@code BuiltInFuncs} and {@code TypeResolver} are
 * independent axes, taken as the core actually needs them. The integration harness injects each by
 * parameter TYPE, so the order above is convention rather than contract — but the count IS
 * enforced: {@code IntegrationProcessor.solelyConstructorOf} throws when a core declares more than
 * one constructor, of any visibility.
 *
 * @param <E> the concrete event kind this processor operates on
 */
public interface EventProcessor<E extends com.webaction.event.Event>
        extends ConvertingEventProcessor<E, E> {

    /**
     * Transforms one inbound event into 0..N outbound events, in emit order.
     *
     * <p>Redeclared rather than merely inherited, so this contract still reads whole at the type
     * most OPs implement. Same signature as {@link ConvertingEventProcessor#processEvent} with both
     * parameters bound to {@code E} — a same-kind transform.</p>
     */
    @Override
    List<E> processEvent(E event);
}
