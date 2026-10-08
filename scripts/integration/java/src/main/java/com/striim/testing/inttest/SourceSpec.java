package com.striim.testing.inttest;

/**
 * The {@code source:} block of a {@code test.yaml}, as it arrives on the request wire
 * (docs/INTEGRATION-TESTS.md): drives the named op as a READER — no input events, one
 * {@code tick()} per step — instead of feeding it a fixture. Jackson-bean shaped (public mutable fields, default
 * no-arg constructor), matching {@link IntegrationProcessor.Request}/{@link UdfSpec}'s own style.
 *
 * <p>The YAML author writes {@code max_ticks:}/{@code expect_events:}; those become
 * {@link #maxTicks}/{@link #expectEvents} on the wire, the same snake-to-camel crossing every
 * other multi-word key in this contract makes.</p>
 *
 * <p><b>There is deliberately no wall-clock field, and adding one would undo the design.</b>
 * {@link #maxTicks} is a COUNT: the caller decides how many ticks happen, so a slow machine takes
 * longer to run the case but cannot change its outcome. A timeout, by contrast, makes the machine
 * a participant in the assertion — which is exactly the flake risk that kept the five readers at
 * {@code integration: null} rather than a tier nobody could trust.</p>
 *
 * <p><b>What the source emits is not declared here.</b> A reader's source is code — a scripted
 * DAO, a seeded emulator — and it reaches the core through the op's own {@code IntegrationSeams}
 * class ({@link IntegrationSeamsLookup}) or through a {@code requires:} service, not through a
 * YAML block that would have to name classes and constructor arguments.</p>
 */
public final class SourceSpec {

    /**
     * The tick budget: how many times {@code tick(<channel>)} may be called. Required, at least 1.
     * With {@link #expectEvents} unset this is also the exact number of ticks driven.
     */
    public Integer maxTicks;

    /**
     * How many events the case expects the source to produce. Optional.
     *
     * <p>When set, ticking stops as soon as this many events have been collected, and a budget
     * exhausted below it fails loudly naming the shortfall — the alternative being a case that
     * asserts against a half-delivered stream and reports a mismatch that has nothing to do with
     * the op. When unset, exactly {@link #maxTicks} ticks are driven, which is what a case
     * proving a quiet source stays quiet needs.</p>
     */
    public Integer expectEvents;

    /**
     * {@code "pre_start"} (default) or {@code "post_start"}: when the case's {@code seed:} SQL is
     * committed relative to this reader starting.
     *
     * <p><b>Why a reader tier needs this and an in-stream tier does not.</b> A snapshot reads what
     * is already there, so seeding first is right. A CHANGE STREAM captures commits made after its
     * start timestamp — data seeded beforehand is invisible to it by design — so a streaming case
     * can only be written if the harness can commit DURING the run. Without it, every streaming
     * case would sit at zero events forever and look like an operator bug.</p>
     */
    public String seedWhen;

    /**
     * Directory for the post-start handshake files, supplied by the Python side.
     *
     * <p>The exchange is deliberately two one-way files rather than a socket or a pipe: the driver
     * is already a subprocess with a shared temp directory, and a file that either exists or does
     * not is the smallest thing that cannot half-arrive. {@code seed.ready} is written by this
     * process once the source has started; {@code seed.go} is written by the caller once its seed
     * SQL has committed.</p>
     */
    public String seedGateDir;

    /** {@code true} when this case wants its seed committed after the source starts. */
    public boolean isPostStartSeed() {
        return "post_start".equals(seedWhen) && seedGateDir != null;
    }

    /**
     * How long to wait for the caller's seed, in milliseconds — supplied by the caller so it is
     * always shorter than the caller's own timeout.
     *
     * <p>A constant here cannot stay correct: a case may set {@code timeout:} freely, and the
     * moment it drops below twice this value the caller expires first and the diagnostic below is
     * never printed. A review caught exactly that with a hardcoded 60s against a 120s default.</p>
     */
    public Long seedGateTimeoutMillis;
}
