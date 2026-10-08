package com.example.common;

import java.util.ArrayList;
import java.util.List;

/**
 * Per-thread collector for non-fatal config issues found by {@link ConfigKeys}.
 *
 * <p>Only a {@code ConfigCheck} main installs a collector. Processor runtime code never does, so in
 * production {@link ConfigKeys} just logs and the collector is inert. The point of collecting is
 * that a verifier can run the parser in non-strict mode, let it reach the end of the file, and then
 * report <em>every</em> unknown key at once instead of dying on the first.</p>
 */
public final class ConfigIssues {

    private static final ThreadLocal<List<String>> COLLECTOR = new ThreadLocal<>();

    private ConfigIssues() {
    }

    /** Begins collecting on this thread, discarding anything already collected. */
    public static void install() {
        COLLECTOR.set(new ArrayList<>());
    }

    /** Returns what was collected and uninstalls. Safe to call when not installed. */
    public static List<String> drain() {
        List<String> collected = COLLECTOR.get();
        COLLECTOR.remove();
        return (collected != null) ? List.copyOf(collected) : List.of();
    }

    /** True when a verifier is gathering issues on this thread. */
    static boolean isCollecting() {
        return COLLECTOR.get() != null;
    }

    /** No-op unless a collector is installed on this thread. */
    static void record(String issue) {
        List<String> collected = COLLECTOR.get();
        if (collected != null)
            collected.add(issue);
    }
}
