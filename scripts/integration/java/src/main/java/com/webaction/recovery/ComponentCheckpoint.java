package com.webaction.recovery;

import java.util.Set;

import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.recovery.ComponentCheckpoint}.
 *
 * <p><b>Why a reader needs this even though the harness never checkpoints.</b> A core that
 * implements {@code common.CheckpointBuilder} declares
 * {@code buildCheckpoint(ComponentCheckpoint, UUID, String)} — and reflecting over a class
 * (the harness's {@code getMethod("start")}) resolves the descriptors of <b>every</b> declared
 * method, not just the one being looked up. Without this class the core cannot be reflected over
 * at all, so it cannot be driven. The method itself is never invoked by the harness: recovery is
 * not expressible in a tier with no restart.</p>
 *
 * <p>Constructors mirror the real class's public shape on
 * 5.4; the accessors are the ones a core can read back off a checkpoint
 * it was handed. Stemmas are stored but not interpreted — the harness has no recovery framework to
 * interpret them with.</p>
 */
public class ComponentCheckpoint {

    private final long timestamp;
    private final UUID componentUuid;
    private final Set<?> stemmas;
    private final String componentState;

    public ComponentCheckpoint(long timestamp, UUID componentUuid) {
        this(timestamp, componentUuid, null);
    }

    public ComponentCheckpoint(long timestamp, UUID componentUuid, Set<?> stemmas) {
        this(timestamp, componentUuid, stemmas, null);
    }

    /** The state-carrying shape OPs persist through the MDR seam (see {@code getComponentState}). */
    public ComponentCheckpoint(long timestamp, UUID componentUuid, Set<?> stemmas,
            String componentState) {
        this.timestamp = timestamp;
        this.componentUuid = componentUuid;
        this.stemmas = stemmas;
        this.componentState = componentState;
    }

    public long getTimestamp() {
        return timestamp;
    }

    public UUID getComponentUuid() {
        return componentUuid;
    }

    /**
     * Declared to return {@code Serializable}, not {@code String}, because that is the real
     * class's descriptor: an OP calling it against a narrower stub signature would link fine and
     * die at runtime with NoSuchMethodError in exactly the invisible way every stub here avoids.
     */
    public java.io.Serializable getComponentState() {
        return componentState;
    }

    public boolean isEmpty() {
        return stemmas == null || stemmas.isEmpty();
    }

    @Override
    public String toString() {
        return "ComponentCheckpoint(ts=" + timestamp + ", component=" + componentUuid + ")";
    }
}
