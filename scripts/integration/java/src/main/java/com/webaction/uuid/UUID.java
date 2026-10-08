package com.webaction.uuid;

import java.util.concurrent.atomic.AtomicLong;

/**
 * Minimal mock of the real Striim {@code com.webaction.uuid.UUID}. Only the surface referenced by
 * the operators this harness drives and by {@code WAEventJsonFactory} is reproduced:
 * a no-arg constructor, {@link #genCurTimeUUID()}, and enough identity/equality
 * behavior to be useful as a map/log value. Not a byte-for-byte reimplementation of
 * the real UUID's time/clock-sequence encoding — nothing in the harness or the
 * operators under test inspects those bits, only the UUID's identity.
 *
 * <p><b>{@code Serializable}, as the real class is.</b> Added when the target tier arrived: a
 * writer's recovery position names the component that produced it, so a UUID travels inside every
 * {@code Position} written to a checkpoint table. Without the interface the checkpoint write fails
 * with a {@code NotSerializableException} raised from inside the writer's own codec — a harness
 * gap that reads as a writer defect.</p>
 */
public class UUID implements java.io.Serializable {

    private static final long serialVersionUID = 1L;

    private static final AtomicLong COUNTER = new AtomicLong();

    public final long time;
    public final long clockSeqAndNode;

    public UUID() {
        this(System.currentTimeMillis(), COUNTER.incrementAndGet());
    }

    public UUID(long time, long clockSeqAndNode) {
        this.time = time;
        this.clockSeqAndNode = clockSeqAndNode;
    }

    public static UUID genCurTimeUUID() {
        return new UUID(System.currentTimeMillis(), COUNTER.incrementAndGet());
    }

    public static UUID nilUUID() {
        return new UUID(0L, 0L);
    }

    public final long getTime() {
        return time;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof UUID other)) {
            return false;
        }
        return time == other.time && clockSeqAndNode == other.clockSeqAndNode;
    }

    @Override
    public int hashCode() {
        return Long.hashCode(time) * 31 + Long.hashCode(clockSeqAndNode);
    }

    @Override
    public String toString() {
        return Long.toHexString(time) + "-" + Long.toHexString(clockSeqAndNode);
    }
}
