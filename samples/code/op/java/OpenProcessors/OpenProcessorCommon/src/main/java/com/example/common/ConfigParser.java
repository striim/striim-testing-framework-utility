package com.example.common;

import java.io.IOException;

/**
 * How {@link ConfigVerifier} invokes one module's config parser — normally an
 * {@code OrmFactory::create} method reference, or a lambda that also supplies the operator
 * properties a config file cannot know.
 *
 * <p>A top-level interface rather than a nested one: OP jars run under a split classloader and must
 * contain no inner classes. Method references and lambdas implementing it compile to
 * {@code invokedynamic}, not to classes, so call sites are fine.</p>
 */
@FunctionalInterface
public interface ConfigParser {

    /**
     * @throws IOException              if the file cannot be read or parsed as JSON
     * @throws IllegalArgumentException if the config is structurally invalid
     */
    void parse(String path) throws IOException;
}
