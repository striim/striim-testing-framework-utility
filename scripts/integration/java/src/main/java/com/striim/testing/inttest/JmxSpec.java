package com.striim.testing.inttest;

/**
 * The request-wire form of a case's {@code assert.jmx:} block (docs/INTEGRATION-TESTS.md).
 * Present only when the case asserts JMX; its absence leaves the drive unchanged.
 *
 * <p>The snapshot goes to {@link #outputFile}, a sidecar, rather than into the request's
 * {@code outputFile}: that file is a bare event list which every existing caller parses as one,
 * and a sidecar leaves it byte-identical.</p>
 */
public final class JmxSpec {

    /**
     * Optional MBean class: a simple name resolved in the core's package, or a fully qualified
     * name. Absent, the bean is discovered ({@link JmxSnapshot}).
     */
    public String bean;

    /** Where the snapshot JSON is written. Required. */
    public String outputFile;
}
