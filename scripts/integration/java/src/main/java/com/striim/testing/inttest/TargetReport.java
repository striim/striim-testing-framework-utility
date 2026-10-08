package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.List;

/**
 * What a {@code target:} run writes to its output file, in place of the emitted events an
 * OpenProcessor case writes there.
 *
 * <p>A target emits nothing, so this carries the two observable outputs that live inside the JVM:
 * what it acknowledged, and the position it reports as durable. The third — the target database
 * itself — is asserted in SQL from the Python side, against the real database the writer just
 * wrote to.</p>
 */
public final class TargetReport {

    /** How many events were handed to the writer. */
    public int eventsAccepted;

    /** How many events the writer acknowledged, across every restart. */
    public int ackedEvents;

    /**
     * Whether the writer carries the {@code Acknowledgeable} marker.
     *
     * <p>False makes an {@code ackedEvents} of 0 mean something quite different: not "this case
     * drove the no-recovery path" but "the platform would never call this writer back at all".
     * The two are indistinguishable from the count alone.</p>
     */
    public boolean acknowledgeable;

    /** Every position released, in order; empty on the no-recovery path. */
    public List<String> ackedPositions = new ArrayList<>();

    /** What the writer reported as durably committed at the end of the run, or null. */
    public String durablePosition;

    /** What it reported immediately BEFORE the restart, or null when the case did not restart. */
    public String durablePositionBeforeRestart;

    /** How many times the writer was restarted. */
    public int restarts;

    /**
     * What the writer published to the platform's monitor, name -> value.
     *
     * <p>The figures a person reads off a monitor page. §43.24 records the platform seeing no
     * throughput at all for a whole live run, with 558 unit tests and a green live run silent
     * about it, because nothing drove {@code publishMonitorEvents}. This is that drive.</p>
     */
    public java.util.Map<String, String> monitor = new java.util.LinkedHashMap<>();

    /**
     * How many events were fed a second time because the restart resumed below them.
     *
     * <p>Zero would mean the restart replayed nothing, which for a writer that batches is the
     * suspicious answer: {@code close()} does not flush, so a restart mid-batch should always
     * leave unacked work to resume.</p>
     */
    public int eventsReplayed;
    /**
     * What the writer handed to the exception store: one entry per
     * notification, each the reason, the failure's text and the events' ordinals in the input --
     * so a case can assert WHICH source events a skipped row carried.
     */
    public List<ExceptionStoreEntry> exceptionStore = new ArrayList<>();

    /** One exception-store notification, as the report carries it. */
    public static final class ExceptionStoreEntry {
        public String reason;
        public String cause;
        public List<Integer> events = new ArrayList<>();
    }
}
