package com.webaction.source.lib.prop;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Mock of the platform's {@code com.webaction.source.lib.prop.Property} — the typed view over a
 * component's configuration map.
 *
 * <p>Only the accessors a sample writer reaches through {@code AbstractWriterApp} are here:
 * {@link #parseRetryPolicy}, which the base delegates {@code getRetryPolicy} to, plus the typed
 * getters. The platform class also declares upwards of eighty {@code String} constants naming
 * reader properties; none is referenced by anything this tier drives, so they are omitted rather
 * than transcribed — a constant this mock got wrong would be worse than one it does not have.</p>
 */
public class Property {

    /** Platform default when {@code ConnectionRetryPolicy} is absent. */
    public static final String DEFAULT_RETRY_POLICY = "retryInterval=30, maxRetries=3";
    /** Key naming the wait, in SECONDS, inside a retry-policy string. */
    public static final String RETRY_WAIT = "retryInterval";
    /** Key naming the attempt count inside a retry-policy string. */
    public static final String RETRY_COUNT = "maxRetries";

    private final Map<String, Object> properties;

    /** A view over {@code properties}; a null map reads as empty. */
    public Property(Map<String, Object> properties) {
        this.properties = properties == null ? new LinkedHashMap<>() : properties;
    }

    /** The underlying map. */
    public Map<String, Object> getMap() {
        return properties;
    }

    /** {@code key} as a string, or {@code fallback} when absent or null. */
    public String getString(String key, String fallback) {
        Object value = properties.get(key);
        return value == null ? fallback : String.valueOf(value);
    }

    /** {@code key} as a boolean, or {@code fallback} when absent or unparseable. */
    public boolean getBoolean(String key, boolean fallback) {
        Object value = properties.get(key);
        if (value instanceof Boolean) {
            return (Boolean) value;
        }
        return value == null ? fallback : Boolean.parseBoolean(String.valueOf(value));
    }

    /** {@code key} as an int, or {@code fallback} when absent or unparseable. */
    public int getInt(String key, int fallback) {
        Object value = properties.get(key);
        if (value instanceof Number) {
            return ((Number) value).intValue();
        }
        if (value == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(String.valueOf(value).trim());
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    /**
     * Parses the retry policy named by {@code key}, e.g. {@code "retryInterval=30, maxRetries=3"}.
     *
     * <p>The interval is stated in SECONDS and stored in MILLISECONDS — see {@link RetryPolicy}.
     * An absent or empty property yields the platform's own {@link #DEFAULT_RETRY_POLICY}; a
     * malformed one raises {@link IllegalArgumentException}, which is the behaviour
     * {@code AbstractWriterApp.getRetryPolicy}'s javadoc says a caller must expect.</p>
     *
     * @param key the property naming the policy
     * @return the parsed policy
     * @throws IllegalArgumentException when the value is present but not parseable
     */
    public RetryPolicy parseRetryPolicy(String key) {
        String raw = getString(key, null);
        if (raw == null || raw.trim().isEmpty()) {
            raw = DEFAULT_RETRY_POLICY;
        }
        int waitSeconds = -1;
        int maxRetries = -1;
        for (String part : raw.split(",")) {
            String[] kv = part.split("=", 2);
            if (kv.length != 2) {
                throw new IllegalArgumentException("Invalid " + key + ": " + raw
                        + " -- supported format is {retryInterval=30, maxRetries=3}");
            }
            String name = kv[0].trim();
            String value = kv[1].trim();
            try {
                if (RETRY_WAIT.equalsIgnoreCase(name)) {
                    waitSeconds = Integer.parseInt(value);
                } else if (RETRY_COUNT.equalsIgnoreCase(name)) {
                    maxRetries = Integer.parseInt(value);
                } else {
                    throw new IllegalArgumentException("Invalid " + key + ": unknown key "
                            + name + " -- supported format is {retryInterval=30, maxRetries=3}");
                }
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("Invalid " + key + ": " + name + "=" + value
                        + " is not a number");
            }
        }
        if (waitSeconds < 0 || maxRetries < 0) {
            throw new IllegalArgumentException("Invalid " + key + ": " + raw
                    + " -- supported format is {retryInterval=30, maxRetries=3}");
        }
        return new RetryPolicy(waitSeconds * 1000, maxRetries);
    }
}
