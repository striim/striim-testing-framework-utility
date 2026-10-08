package com.example;

import com.example.common.WAEvents;
import com.webaction.proc.events.WAEvent;

/**
 * ReferenceUdf — the canonical gold-STANDARD-SHAPE example (docs/TESTING-YOUR-JAVA.md): a minimal
 * WAEvent-aware UDF demonstrating the required shape every UDF must have, analogous to
 * OpenProcessors/ReferenceOp for the OP template. Not a product feature — a module generator
 * can copy its logging-toggle trio when scaffolding a new UDF, and it is the diff baseline a
 * consistency review can fall back on when a real UDF's business logic gets in the way of seeing
 * the required shape.
 *
 * <p>{@link #ReferenceUdfMarkProcessed} is the UDF analog of ReferenceOp's
 * {@code copy-adds-userdata} sample: it copies the incoming event via {@link WAEvents#copyEvent} —
 * NOT {@code WAEvent.makeCopy}, which silently drops the {@code SimpleEvent} fields the seam then
 * re-stamps; that is what the seam is for, so callers stop reaching for {@code makeCopy} — and
 * stamps {@code userdata.processed=true} on the copy, touching nothing else — {@code data[]}/
 * {@code before[]} are byte-identical to the source. Deliberately the SAME single mutation
 * ReferenceOp's {@code Processor.processEvent} performs, so the two teaching exemplars stay
 * consistent at the OP and UDF layers. A real UDF replaces the body of a function like this one
 * with an actual transform.
 */
public class ReferenceUdf {

    // --- logging toggle (docs/TESTING-YOUR-JAVA.md) ---

    /**
     * When true, each public method writes its input/output to stdout. Off by default; toggle at
     * runtime with {@link #ReferenceUdfSetLogging(boolean)} (no rebuild required). Declared
     * volatile so a runtime toggle is visible across the threads Striim runs CQs on. Caught
     * exceptions are logged to stderr regardless of this flag.
     */
    private static volatile boolean enableLogging = false;

    /**
     * Enables or disables diagnostic input/output logging for all ReferenceUdf functions at
     * runtime. Returns the value set so the call can be used inside a TQL SELECT expression.
     *
     * @param enabled true to turn logging on, false to turn it off
     * @return the value just set
     */
    public static boolean ReferenceUdfSetLogging(final boolean enabled) {
        enableLogging = enabled;
        return enabled;
    }

    /**
     * Logs a method's input and output to stdout when logging is enabled.
     */
    private static void logTransform(final String method, final WAEvent in, final WAEvent out) {
        if (enableLogging) {
            System.out.println("[ReferenceUdf] " + method + " in : " + in);
            System.out.println("[ReferenceUdf] " + method + " out: " + out);
        }
    }

    /**
     * Logs a caught exception to stderr. Always logs, independent of the logging flag, so a
     * fail-safe that swallows an exception cannot silently hide a data problem.
     */
    private static void logError(final String method, final Exception ex) {
        System.err.println("[ReferenceUdf] " + method + " error: " + ex);
        ex.printStackTrace();
    }

    // --- transform ---

    /**
     * Copies the incoming event ({@link WAEvents#copyEvent}) and stamps
     * {@code userdata.processed=true} on the copy — the UDF analog of ReferenceOp's pure
     * copy-through-plus-stamp enricher. Null-safe (a null input returns null, the mutator
     * fail-safe value) and never throws. The source event is never
     * mutated; only the returned copy carries the flag.
     *
     * @param in the input event, may be null
     * @return a copy of {@code in} with {@code userdata.processed} set to {@code "true"}, or
     *         {@code null} if {@code in} is null
     */
    public static WAEvent ReferenceUdfMarkProcessed(final WAEvent in) {
        try {
            if (in == null) {
                logTransform("ReferenceUdfMarkProcessed", null, null);
                return null;
            }
            final WAEvent copy = WAEvents.copyEvent(in);
            copy.putUserdata("processed", "true");
            logTransform("ReferenceUdfMarkProcessed", in, copy);
            return copy;
        } catch (final Exception ex) {
            logError("ReferenceUdfMarkProcessed", ex);
            return in;
        }
    }
}
