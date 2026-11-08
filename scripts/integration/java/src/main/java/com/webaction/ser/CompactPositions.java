package com.webaction.ser;

import com.striim.testing.inttest.OrdinalPosition;
import com.webaction.recovery.Path;
import com.webaction.recovery.Position;
import com.webaction.uuid.UUID;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/**
 * {@link KryoSingleton}'s compact form for a {@code Position} whose paths are all the harness's
 * {@code OrdinalPosition}s: near the size the platform's registered-class Kryo writes, rather than
 * Java serialization's ~700 bytes a path (see {@link KryoSingleton}).
 *
 * <p>Layout: {@code 'P'} (Java serialization starts with {@code 0xAC}), version 1, the path count;
 * per path a flags byte (1 component, 2 distribution, 4 a high other than the low, 8 a null
 * high), the component's two longs, the distribution (UTF), the low ordinal and, with flag 4,
 * the high ordinal.</p>
 */
final class CompactPositions {

    private static final int MAGIC = 'P';
    private static final int VERSION = 1;

    private static final int COMPONENT = 1;
    private static final int DISTRIBUTION = 2;
    private static final int SEPARATE_HIGH = 4;
    private static final int NULL_HIGH = 8;

    private CompactPositions() {
    }

    static boolean isCompact(final byte[] bytes) {
        return bytes != null && bytes.length >= 2 && bytes[0] == MAGIC && bytes[1] == VERSION;
    }

    /** @return the compact bytes, or null when {@code value} is not a position this form covers */
    static byte[] write(final Object value) {
        if (value == null || value.getClass() != Position.class) {
            return null;
        }
        final Position position = (Position) value;
        for (final Path p : position.values()) {
            if (p.getClass() != Path.class || !(p.getLowSourcePosition() instanceof OrdinalPosition)
                    || p.getHighSourcePosition() != null
                    && !(p.getHighSourcePosition() instanceof OrdinalPosition)) {
                return null;
            }
        }
        try {
            final ByteArrayOutputStream bytes = new ByteArrayOutputStream(8 + 40 * position.size());
            final DataOutputStream out = new DataOutputStream(bytes);
            out.writeByte(MAGIC);
            out.writeByte(VERSION);
            out.writeInt(position.size());
            for (final Path p : position.values()) {
                final OrdinalPosition low = (OrdinalPosition) p.getLowSourcePosition();
                final OrdinalPosition high = (OrdinalPosition) p.getHighSourcePosition();
                final int flags = (p.getComponentID() != null ? COMPONENT : 0)
                        | (p.getDistributionID() != null ? DISTRIBUTION : 0)
                        | (high == null ? NULL_HIGH : high != low ? SEPARATE_HIGH : 0);
                out.writeByte(flags);
                if (p.getComponentID() != null) {
                    out.writeLong(p.getComponentID().time);
                    out.writeLong(p.getComponentID().clockSeqAndNode);
                }
                if (p.getDistributionID() != null) {
                    out.writeUTF(p.getDistributionID());
                }
                out.writeLong(low.ordinal());
                if ((flags & SEPARATE_HIGH) != 0) {
                    out.writeLong(high.ordinal());
                }
            }
            out.flush();
            return bytes.toByteArray();
        } catch (final IOException e) {
            throw new IllegalStateException(e);
        }
    }

    static Position read(final byte[] bytes) {
        try (DataInputStream in = new DataInputStream(new ByteArrayInputStream(bytes))) {
            in.readByte();
            in.readByte();
            final int n = in.readInt();
            final List<Path> paths = new ArrayList<>(n);
            for (int i = 0; i < n; i++) {
                final int flags = in.readByte();
                final UUID component = (flags & COMPONENT) != 0 ? new UUID(in.readLong(), in.readLong()) : null;
                final String distribution = (flags & DISTRIBUTION) != 0 ? in.readUTF() : null;
                final OrdinalPosition low = new OrdinalPosition(in.readLong());
                final OrdinalPosition high = (flags & NULL_HIGH) != 0 ? null
                        : (flags & SEPARATE_HIGH) != 0 ? new OrdinalPosition(in.readLong()) : low;
                paths.add(new Path(component, distribution, low, high));
            }
            return new Position(paths);
        } catch (final IOException e) {
            throw new IllegalStateException("inttest KryoSingleton mock could not decode "
                    + bytes.length + " compact byte(s)", e);
        }
    }
}
