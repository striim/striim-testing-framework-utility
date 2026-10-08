package com.example.common;

import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.uuid.UUID;

/**
 * The recovery seam for a reader in the {@code ComponentCheckpoint} family: the core builds the
 * checkpoint, the {@code App} only forwards the framework's own default into it.
 *
 * <p>A reader implements this on its {@code Processor} when its {@code RECOVERY}-clause checkpoint
 * is a hand-built {@code ComponentCheckpoint}. A reader with no checkpoint of its own does not
 * implement it; a reader that pushes a plain {@code SourcePosition} through {@link SourceChannel}
 * uses that instead (see {@link Emitter}).</p>
 *
 * <p><b>A reader can belong to both families at once, which is why the seam lives where it
 * does.</b> Its {@code App} may extend {@link AbstractSourceApp} — the {@code SourcePosition}
 * family's base — for {@code emit}/identity while its {@code RECOVERY}-clause checkpoint is a
 * hand-built {@code ComponentCheckpoint}. {@link AbstractCheckpointSourceApp} cannot host that,
 * being {@code AbstractSourceApp}'s sibling rather than its ancestor, so the
 * {@code getCheckpoint()} body lives on their common parent, {@link
 * AbstractReaderApp#getCheckpoint()}, reached through the {@link
 * AbstractReaderApp#checkpointCore()} hook that both siblings' subclasses can override.</p>
 *
 * <p><b>Why this is an interface rather than a method on a base class.</b> An {@code App} holds
 * its core as the module's own {@code Processor} type, so the shared {@code getCheckpoint()} in
 * {@link AbstractReaderApp} can only reach {@code buildCheckpoint} through a type it knows. This is
 * that type.</p>
 */
public interface CheckpointBuilder {

    /**
     * Builds the component's checkpoint, given the framework's default.
     *
     * @param defaultCheckpoint what {@code SourceProcess.getCheckpoint()} would have returned;
     *     an implementation that has nothing of its own to record returns it unchanged
     * @param sourceUUID        the deployed source's framework identity
     * @param distributionID    the deployed source's distribution id
     */
    ComponentCheckpoint buildCheckpoint(ComponentCheckpoint defaultCheckpoint, UUID sourceUUID,
            String distributionID);
}
