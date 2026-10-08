package com.webaction.common.exc;

/**
 * Mirror of the platform's {@code com.webaction.common.exc.ConnectionException}.
 *
 * <p>⚠ <b>This class is the entire reconnect contract</b> (R2, §126). `RetriableWriter.handleEvent`
 * wraps `processEvent` in two handlers: this type routes to {@code onConnectionException} →
 * {@code reconnect()} → {@code cleanup()} plus the adapter's own {@code init()}; every other
 * {@code Exception} is rethrown and the application halts. A writer signals "the connection is
 * gone, please reconnect" by throwing this and nothing else.</p>
 *
 * <p>⚠ It is here because the writer NAMES it, and a mocked classpath that omits a class the
 * operator references fails at {@code Class.forName} with {@code NoClassDefFoundError} before a
 * single event is driven — which is exactly how its absence was found (§126.5), by the integration
 * tier, after 642 unit tests passed against the real jars.</p>
 */
public class ConnectionException extends SystemException {

    private static final long serialVersionUID = 1L;

    public ConnectionException() {
        super();
    }

    public ConnectionException(final String message) {
        super(message);
    }

    public ConnectionException(final Throwable cause) {
        super(cause);
    }

    public ConnectionException(final String message, final Throwable cause) {
        super(message, cause);
    }

    public ConnectionException(final String message, final Throwable cause,
                               final boolean suppression, final boolean writableStackTrace) {
        super(message, cause, suppression, writableStackTrace);
    }
}
