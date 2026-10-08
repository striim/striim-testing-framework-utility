package com.example.common;

import java.io.PrintStream;
import java.time.Instant;
import java.util.function.Supplier;

/**
 * Shared log wrapper, hoisted verbatim from the per-OP copies. Concrete, not an interface: the goal is one
 * implementation whose backend can later be swapped — to log4j, say — without touching any call
 * site. Always injected into the core.
 */
public class Logger {

    private final String tag;
    private final boolean enableDebugLogging;
    private final boolean enableErrorLogging;

    public Logger(String tag, boolean enableDebugLogging) {
        this(tag, enableDebugLogging, true); // errors enabled by default
    }

    public Logger(String tag, boolean enableDebugLogging, boolean enableErrorLogging) {
        this.tag = tag;
        this.enableDebugLogging = enableDebugLogging;
        this.enableErrorLogging = enableErrorLogging;
    }

    /** Convenience factory equivalent to the two-arg constructor; errors stay enabled. */
    public static Logger of(String tag, boolean enableDebug) {
        return new Logger(tag, enableDebug);
    }

    /**
     * DEBUG on stdout, emitted only when debug logging is enabled.
     *
     * <p>Takes a supplier so the message is not built when the gate is closed — the reason this is
     * safe to call on a per-event path.</p>
     */
    public void log(Supplier<String> messageSupplier) {
        if (enableDebugLogging)
            emit("DEBUG", System.out, messageSupplier);
    }

    /**
     * Unconditional info logging — not gated by the debug flag. Used for bootstrap
     * progress/completion and periodic metrics reporting.
     */
    public void logAlways(Supplier<String> messageSupplier) {
        emit("INFO", System.out, messageSupplier);
    }

    /**
     * A defect the caller tolerated rather than one that stopped it. On stderr so it survives
     * whatever is filtering stdout, but distinct from ERROR so it does not trip error alerting.
     * Gated by the same flag as {@link #logError}: a caller that deliberately silenced a Logger
     * should not still get stderr output.
     */
    public void logWarn(Supplier<String> messageSupplier) {
        if (enableErrorLogging)
            emit("WARN", System.err, messageSupplier);
    }

    /**
     * ERROR on stderr, gated by the error flag rather than the debug one.
     *
     * <p>Distinct from {@link #logWarn} so that a genuine failure trips error alerting and a
     * tolerated one does not.</p>
     */
    public void logError(Supplier<String> messageSupplier) {
        if (enableErrorLogging)
            emit("ERROR", System.err, messageSupplier);
    }

    private void emit(String level, PrintStream stream, Supplier<String> messageSupplier) {
        if (messageSupplier != null) {
            stream.println(String.format(
                    "%s %s %s (%s): %s",
                    level,
                    Instant.now().toString(),
                    tag,
                    Thread.currentThread().getName(),
                    messageSupplier.get()));
        }
    }
}
