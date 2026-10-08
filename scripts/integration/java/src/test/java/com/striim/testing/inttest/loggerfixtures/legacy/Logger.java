package com.striim.testing.inttest.loggerfixtures.legacy;

/** An older Common's {@code Logger}: two-arg constructor only, no {@code LogSinkSelection} beside it. */
public final class Logger {

    public final String tag;
    public final boolean enableDebugLogging;

    public Logger(String tag, boolean enableDebugLogging) {
        this.tag = tag;
        this.enableDebugLogging = enableDebugLogging;
    }
}
