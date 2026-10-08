package com.webaction.common.exc;

/**
 * Mirror of the platform's {@code com.webaction.common.exc.SystemException}.
 *
 * <p>Matches the platform class's public shape: {@code extends Exception implements
 * Serializable}, with the five constructors it declares. Present only so the mocked classpath can
 * LOAD an operator that references {@link ConnectionException}; nothing here has behaviour.</p>
 */
public class SystemException extends Exception implements java.io.Serializable {

    private static final long serialVersionUID = 1L;

    public SystemException() {
        super();
    }

    public SystemException(final String message) {
        super(message);
    }

    public SystemException(final Throwable cause) {
        super(cause);
    }

    public SystemException(final String message, final Throwable cause) {
        super(message, cause);
    }

    public SystemException(final String message, final Throwable cause,
                           final boolean suppression, final boolean writableStackTrace) {
        super(message, cause, suppression, writableStackTrace);
    }
}
