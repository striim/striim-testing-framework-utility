package com.striim.testing.inttest.loggerfixtures.current;

/** Stands in for OpenProcessorCommon's {@code Logger}: records what the harness built it with. */
public final class Logger {

    public final String tag;
    public final boolean enableDebugLogging;
    public final LogSinkSelection selection;

    public Logger(String tag, boolean enableDebugLogging) {
        this(tag, enableDebugLogging, null);
    }

    public Logger(String tag, boolean enableDebugLogging, LogSinkSelection selection) {
        this.tag = tag;
        this.enableDebugLogging = enableDebugLogging;
        this.selection = selection;
    }
}
