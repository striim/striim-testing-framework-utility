package com.striim.testing.inttest.loggerfixtures.current;

/** Stands in for OpenProcessorCommon's {@code LogSinkSelection}: records the arguments to {@code resolve}. */
public final class LogSinkSelection {

    public final Object property;
    public final String moduleDefault;

    private LogSinkSelection(Object property, String moduleDefault) {
        this.property = property;
        this.moduleDefault = moduleDefault;
    }

    public static LogSinkSelection resolve(Object property, String moduleDefault) {
        return new LogSinkSelection(property, moduleDefault);
    }
}
