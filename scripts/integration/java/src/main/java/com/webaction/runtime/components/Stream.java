package com.webaction.runtime.components;

import com.webaction.runtime.channels.Channel;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.Stream}: here, only the server's
 * exception stream, which {@code NotifyExceptionStore.getExceptionStream()} returns and
 * {@code ExceptionStorePublisher} publishes on through {@link #getChannel()}.
 *
 * <p>The real class extends {@code FlowComponent}, as this one does, and is built from a
 * {@code MetaInfo$Stream} by the server. Its data-flow members (receive, delay buffer, show
 * streams, checkpoints) are omitted: nothing in this tier routes events through a stream.</p>
 */
public class Stream extends FlowComponent {

    private final Channel channel;

    /**
     * Harness-only. The real constructor takes the stream's metadata, a {@code BaseServer}, its
     * flow and a recovery flag; module code never constructs a Stream.
     */
    public Stream(final Channel channel) {
        this.channel = channel;
    }

    /** The channel events published to this stream go out on; null when it has none. */
    public Channel getChannel() {
        return channel;
    }
}
