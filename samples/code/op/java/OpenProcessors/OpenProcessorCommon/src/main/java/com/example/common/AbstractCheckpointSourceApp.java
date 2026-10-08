package com.example.common;

/**
 * Shared {@code processor} slot for a reader {@code App} in the {@code ComponentCheckpoint}
 * family. The subclass keeps its own {@code init}/{@code close}/emit shape and only
 * assigns {@link #processor}; the {@code getCheckpoint()} body itself now lives on
 * {@link AbstractReaderApp}, reached through {@link #checkpointCore()} below.
 *
 * <p><b>What this removes, exactly.</b> Two reader modules declared
 * byte-identical five-line {@code getCheckpoint()} bodies. Five lines is a small thing to hoist,
 * and line count is <i>not</i> the argument for hoisting it — <b>this is recovery code</b>. A
 * silent divergence between two copies of it surfaces as a checkpoint that is wrong after a crash,
 * which is among the most expensive failure modes an OP can have and among the least likely to be
 * caught by a test that does not restart anything.</p>
 *
 * <p><b>⚠ The {@code getCheckpoint()} that used to live here moved to {@link AbstractReaderApp}.</b>
 * A third reader's {@code App} needed the exact same body but
 * descends through the sibling {@link AbstractSourceApp} instead, so it could not extend this class
 * too. The body is now {@link AbstractReaderApp#getCheckpoint()}, driven by the {@link
 * #checkpointCore()} hook every reader can override regardless of which sibling it
 * descends through; this class supplies the hook from {@link #processor}. The {@code processor ==
 * null} guard that used to be declared here — load-bearing, because the framework may call this
 * before {@code init()} has built the core — is now enforced once, in the shared method.</p>
 *
 * <p><b>Re-parented onto {@link AbstractReaderApp}.</b> It still extends
 * {@code SourceProcess}, transitively — the change makes the shared flow-identity accessors
 * ({@code appQualifiedName()}, {@code componentQualifiedName()}) reachable from this family too,
 * on the same footing as the {@code SourcePosition} family's {@link AbstractSourceApp}. No
 * observable behaviour changes.</p>
 *
 * <p><b>Deliberately NOT here:</b> {@code init}, {@code close}, {@code receiveImpl}, and the emit
 * seam. Those differ across readers in ways that are real — one reader's
 * {@code close()} is deliberately unsynchronized, and its output channel lives on its own
 * {@code App} because {@link Emitter} declares {@code emit(E)} and carries none. A base class that
 * forced them into one shape is precisely how {@link Emitter} came to ship with five hand-rolled
 * copies and no consumers.</p>
 *
 * @param <P> the module's own core type, which supplies the checkpoint
 */
public abstract class AbstractCheckpointSourceApp<P extends CheckpointBuilder> extends AbstractReaderApp {

    /**
     * The module's core, assigned by the subclass's {@code init}.
     *
     * <p><b>{@code volatile} because recovery reads it from another thread.</b> {@code init()}
     * writes this under the subclass's own {@code synchronized}, but {@link #checkpointCore()} is
     * called by the framework (via {@link AbstractReaderApp#getCheckpoint()}) without holding that
     * monitor.</p>
     */
    protected volatile P processor;

    /** Feeds {@link #processor} to the shared {@link AbstractReaderApp#getCheckpoint()} seam. */
    @Override
    protected CheckpointBuilder checkpointCore() {
        return processor;
    }
}
