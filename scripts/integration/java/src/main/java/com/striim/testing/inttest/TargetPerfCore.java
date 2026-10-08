package com.striim.testing.inttest;

import java.io.File;
import java.util.List;
import java.util.Map;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

/**
 * Drives a TARGET for the perf tier: {@code accept} per event, one {@code flush} per window.
 *
 * <p><b>T3.</b> The perf tier could not drive a target at all — {@code perfmanifest.py} had no
 * {@code target:} key and this driver had no target path — so a writer's throughput had never been
 * measured by any tier. Every perf case before this one drives an OpenProcessor or a reader.</p>
 *
 * <p><b>A target emits nothing</b>, so {@link #processEvent} returns an empty list and the
 * measured "output events" of a target case are always zero. That is not a defect in the number:
 * it is what a writer is. What the tier measures instead is <b>input events through a window that
 * ends in a flush</b> — see {@link EventDriver#endOfWindow()} and §64.3.</p>
 *
 * <p><b>Positions are attached</b>, one per event, exactly as {@code IntegrationProcessor} does:
 * without them the writer takes its no-recovery path, which acks differently and skips the
 * checkpoint write — so measuring without positions would measure a configuration nobody runs.</p>
 */
final class TargetPerfCore implements EventDriver {

    private final TargetCore core;
    private final String distributionId;
    private long ordinal;

    private TargetPerfCore(final TargetCore core, final String distributionId) {
        this.core = core;
        this.distributionId = distributionId;
    }

    /**
     * @param opJar              the writer's jar
     * @param properties         its property map, already token-substituted
     * @param types              the source-schema map, as the request carries it
     * @param passwordProperties property keys to wrap in the mock {@code Password}
     */
    static TargetPerfCore build(final File opJar, final Map<String, Object> properties,
                                final Map<String, Object> types,
                                final List<String> passwordProperties,
                                final String distributionId,
                                final List<WAEvent> templates) throws Exception {
        final String distribution = distributionId == null ? "perftest" : distributionId;
        final SourceTypes sourceTypes = SourceTypes.from(types);
        // ⚠ STAMPED BEFORE THE CORE IS BUILT, exactly as IntegrationProcessor does it. Without
        // this the writer resolves a type it was never given and fails with "the source type for
        // <table> declares 0 field(s)" -- a message about the TARGET table that is really about
        // the events never having been told which source type they are.
        sourceTypes.stamp(templates);
        final TargetCore core = TargetCore.build(opJar, properties, passwordProperties,
                distribution, sourceTypes, true);
        return new TargetPerfCore(core, distribution);
    }

    @Override
    public List<WAEvent> processEvent(final Event event) throws Exception {
        ordinal++;
        core.accept(EventDriver.requireWAEvent(event, "a target: case"),
                new com.webaction.recovery.ImmutableStemma(
                POSITION_COMPONENT, distributionId, new OrdinalPosition(ordinal)));
        // A target emits nothing. Returning an immutable empty list rather than null keeps the
        // perf loop's `emitted.size()` arithmetic identical for every driver.
        return List.of();
    }

    @Override
    public void endOfWindow() throws Exception {
        // THE MEASUREMENT. accept() only accumulates; this is where the round trips, the
        // transaction and the checkpoint happen, and it is called inside the timed region.
        core.flush();
    }

    @Override
    public void close() throws Exception {
        core.close();
    }

    /** Matches {@code IntegrationProcessor}'s, so a position means the same thing in both tiers. */
    private static final UUID POSITION_COMPONENT = new UUID(0L, -1L);
}
