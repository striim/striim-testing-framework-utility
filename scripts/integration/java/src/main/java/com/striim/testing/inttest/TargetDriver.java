package com.striim.testing.inttest;

import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;

/**
 * The seam a TARGET is driven through — and the reason it is not a fourth {@link EventDriver}.
 *
 * <p>{@code EventDriver} is "one event in, zero-or-more out"; a target emits none. Its observable
 * output is the <b>target database</b>, the <b>checkpoint row</b>, and <b>what it acknowledged</b>,
 * so the contract has to be shaped around those three rather than around a return value.
 * This interface exposes all three.</p>
 *
 * <p>{@link #restart()} is why the tier is worth building: "write a window, stop, restart, resume,
 * do not double-write" is not credibly testable against a proxy standing in for a driver, and
 * recovery is the database writer's first stated goal.</p>
 */
interface TargetDriver {

    /**
     * Hands the target one event and the recovery position that pins it.
     *
     * @param event    the event to write
     * @param position the position pinning it, or null for the no-recovery path
     */
    void accept(WAEvent event, ImmutableStemma position) throws Exception;

    /** Drives the platform's flush hook, so anything accumulated is applied and committed. */
    void flush() throws Exception;

    /**
     * What the target reports as durably committed, serialised so a case can assert it.
     *
     * @return the rendered position, or null when the target reports none
     */
    String durablePosition() throws Exception;

    /**
     * Closes the target and builds a fresh one from the same properties.
     *
     * <p><b>Not a fresh JVM.</b> The classloader, and therefore every static the target jar holds
     * — a registered JDBC driver, a cached statement pool — survives. A real restart under Striim
     * reloads the module, so anything this reuses is something the tier does not test. What it
     * does test is the part that matters here: whether the writer, given the same properties and
     * the checkpoint row it left behind, resumes where it stopped rather than replaying.</p>
     */
    void restart() throws Exception;

    /** How many events the target acknowledged, across every restart. */
    int ackedEvents();

    /** Releases the target's resources. */
    void close() throws Exception;
}
