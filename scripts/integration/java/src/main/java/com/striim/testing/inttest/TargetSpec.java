package com.striim.testing.inttest;

/**
 * The {@code target:} block of a {@code test.yaml}, as it arrives on the request wire
 * (docs/INTEGRATION-TESTS.md): drives the named module as a Striim <b>Target</b> — a
 * {@code RetriableWriter} that emits nothing and writes to a real database.
 *
 * <p>Jackson-bean shaped, matching {@link IntegrationProcessor.Request}/{@link SourceSpec}. The
 * YAML author writes {@code restart_after:}; that becomes {@link #restartAfter} on the wire,
 * always as a LIST — the manifest normalises a bare int to a one-element one, so this side
 * never has to branch on the two spellings.</p>
 *
 * <p><b>What a target case asserts is not declared here.</b> A target's output is the target
 * database, the checkpoint row, and what it acknowledged. The first is asserted in SQL from the
 * Python side; the other two come back in the run report this driver writes. This block only says
 * how to DRIVE the writer.</p>
 */
public final class TargetSpec {

    /**
     * Restart the writer after this many events, then feed the rest. Optional; null never restarts.
     *
     * <p>This is the tier's reason for existing. {@code close()} does not flush, so the events
     * accumulated at the restart point were never applied and never acked — their positions stay
     * pinned and the source replays them. A case sets this, replays the whole input, and asserts
     * the target holds each row exactly once. Nothing below this tier can ask that question:
     * a unit test's fake driver has no state to survive the restart.</p>
     */
    public java.util.List<Integer> restartAfter;

    /**
     * Event ordinals after which this driver PAUSES so the caller can run SQL against the live
     * target. YAML author writes {@code mid_run: [{after: N, file: …}]}; only the ordinals cross
     * the wire, because the caller runs the SQL and the driver only has to stop.
     *
     * <p>The rendezvous is the one {@code SourceSpec.seedGateDir} already documents — two one-way
     * files, because a file either exists or does not and cannot half-arrive — but per ORDINAL
     * rather than once: {@code midrun.N.ready} is written here, {@code midrun.N.go} by the
     * caller.</p>
     *
     * <p>This is what makes two otherwise unreachable diagnostics testable end to end: the target
     * ALTERed underneath a running writer ({@code verifyUnchanged}), and a restart after a
     * FAILURE rather than a clean stop.</p>
     */
    public java.util.List<Integer> midRunAfter;

    /** Directory holding the mid-run rendezvous files; null disables the hook. */
    public String midRunGateDir;

    /** How long to wait for the caller at each gate. Supplied by the caller from its own budget. */
    public Long midRunTimeoutMillis;

    /**
     * Attach a synthetic position to each event. Default true.
     *
     * <p>Set false to drive the NO-RECOVERY path, where every event arrives with a null position
     * and the writer's {@code isRecoveryEnabled} is false.
     * In this path, a count-only ack is the whole of what the platform sees.</p>
     */
    public Boolean positions;

    /** {@code true} unless the case explicitly asked for the no-recovery path. */
    public boolean withPositions() {
        return positions == null || positions;
    }

    /**
     * The distribution key the platform would hand {@code init}. Optional; defaults to
     * {@code "inttest"}.
     */
    public String distributionId;
}
