package com.webaction.exceptionhandling;

import com.webaction.runtime.components.EntityType;

/**
 * Mock of the platform's {@code com.webaction.exceptionhandling.ExceptionType}: the class an
 * {@code ExceptionEvent} files an exception under, which an {@code EXCEPTIONHANDLER} rule
 * matches. Every constant of the real enum on 5.4.0.6G, in its order.
 *
 * <p>{@link #getType} and {@link #getExceptionType} are the real methods (cfr): an exception is
 * classed by its simple class name when that names a constant, else as
 * {@link #AdapterException} for a source or target and {@link #UnknownException} otherwise.</p>
 */
public enum ExceptionType {
    ArithmeticException,
    NumberFormatException,
    ClassCastException,
    InvalidDataException,
    NullPointerException,
    SystemException,
    ConnectionException,
    AdapterException,
    UnExpectedDDLException,
    StriimConnectException,
    UnknownException;

    public static ExceptionType getType(String val, EntityType entityType) {
        for (ExceptionType et : values()) {
            if (et.name().equalsIgnoreCase(val)) {
                return et;
            }
        }
        if (entityType == EntityType.SOURCE || entityType == EntityType.TARGET) {
            return AdapterException;
        }
        return UnknownException;
    }

    public static ExceptionType getExceptionType(Throwable ex, EntityType entityType) {
        if (ex == null) {
            return UnknownException;
        }
        return getType(ex.getClass().getSimpleName(), entityType);
    }
}
