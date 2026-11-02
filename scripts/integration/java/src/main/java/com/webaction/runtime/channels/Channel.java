package com.webaction.runtime.channels;

import com.webaction.runtime.containers.ITaskEvent;

/**
 * Mock of the platform's {@code com.webaction.runtime.channels.Channel}: what a stream publishes
 * on. {@code ExceptionStorePublisher} publishes a skipped row's {@code ExceptionEvent} on the
 * exception stream's channel.
 *
 * <p>Only {@link #publish} is declared, with the real signature. The real interface also extends
 * {@code Closeable} and {@code Monitorable} and manages subscribers; nothing in this tier
 * subscribes, so those members are omitted rather than stubbed.</p>
 */
public interface Channel {

    void publish(ITaskEvent event) throws Exception;
}
