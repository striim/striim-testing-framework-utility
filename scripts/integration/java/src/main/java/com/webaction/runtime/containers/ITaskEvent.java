package com.webaction.runtime.containers;

import java.io.Serializable;

/**
 * Mock of the platform's {@code com.webaction.runtime.containers.ITaskEvent}: the unit a
 * {@code Channel} publishes. {@code ExceptionStorePublisher} hands one to the exception stream's
 * channel, and a stand-in channel reads the events back out of {@link #batch()}.
 *
 * <p>Only {@link #batch()} is declared, with the real signature. The real interface also extends
 * {@code KryoSerializable} and carries window, snapshot and lag members; nothing in this tier
 * serialises a task event or windows one, so they are omitted rather than stubbed.</p>
 */
public interface ITaskEvent extends Serializable {

    /** The events this task event adds. */
    IBatch<WAEvent> batch();
}
