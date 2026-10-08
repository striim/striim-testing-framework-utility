package com.example.common;

import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.uuid.UUID;

/**
 * Read/write seam over the MDR component-checkpoint table. The production implementation is
 * {@link MdrComponentStateStore}; tests inject an in-memory implementation instead.
 */
public interface ComponentStateStore {

    /** The latest persisted checkpoint for the component, or null when none exists or lookup failed. */
    ComponentCheckpoint get(UUID componentUuid);

    /** Persists a checkpoint. True when durably saved. */
    boolean put(ComponentCheckpoint checkpoint);
}
