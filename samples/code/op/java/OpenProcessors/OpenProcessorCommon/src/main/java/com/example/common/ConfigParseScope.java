package com.example.common;

/**
 * One config parse in progress: its posture, what it is reading, and how much it has tolerated so
 * far.
 *
 * <p>Exists so {@link ConfigKeys} can emit a single summary line per config load. Per-location WARN
 * lines are greppable but give no per-app total, so answering "how much stray-key material does this
 * deployment carry" means counting lines across servers. One summary line per load makes it a single
 * grep.</p>
 *
 * <p>A top-level class, not a nested one — OP jars run under a split classloader and must contain no
 * inner classes.</p>
 */
public final class ConfigParseScope {

    final boolean rejecting;
    final String source;
    int keys;
    int shapes;
    int locations;

    ConfigParseScope(boolean rejecting, String source) {
        this.rejecting = rejecting;
        this.source = source;
    }
}
