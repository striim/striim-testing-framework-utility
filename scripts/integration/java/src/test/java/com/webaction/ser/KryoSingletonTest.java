package com.webaction.ser;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.striim.testing.inttest.OrdinalPosition;
import com.webaction.recovery.JSONSourcePosition;
import com.webaction.recovery.Path;
import com.webaction.recovery.Position;
import com.webaction.uuid.UUID;
import java.io.ByteArrayOutputStream;
import java.io.ObjectOutputStream;
import java.util.Arrays;
import org.junit.jupiter.api.Test;

class KryoSingletonTest {

    private static final UUID C = new UUID(0L, -1L);

    private static Position roundTrip(final Position p) {
        return (Position) KryoSingleton.read(KryoSingleton.write(p, false), false);
    }

    @Test
    void anOrdinalPositionIsWrittenCompactlyAndReadsBackEqual() {
        final Position p = new Position(new Path(C, "perftest", new OrdinalPosition(42)));
        final byte[] bytes = KryoSingleton.write(p, false);
        assertTrue(CompactPositions.isCompact(bytes));
        assertTrue(bytes.length < 64, "compact, was " + bytes.length);
        final Position back = roundTrip(p);
        assertEquals(p, back);
        final Path path = back.values().iterator().next();
        assertSame(path.getLowSourcePosition(), path.getHighSourcePosition(), "one ordinal stays one");
    }

    @Test
    void severalPathsASeparateHighANullDistributionAndNullHighRoundTrip() {
        final Position p = new Position(Arrays.asList(
                new Path(C, "a", new OrdinalPosition(1), new OrdinalPosition(9)),
                new Path(new UUID(7L, 8L), null, new OrdinalPosition(3)),
                new Path(null, "b", new OrdinalPosition(5), null)));
        assertTrue(CompactPositions.isCompact(KryoSingleton.write(p, false)));
        final Position back = roundTrip(p);
        assertEquals(p, back);
        assertEquals(p.toPaths(), back.toPaths(), "path order kept");
    }

    @Test
    void anEmptyPositionRoundTrips() {
        assertEquals(new Position(), roundTrip(new Position()));
    }

    @Test
    void anyOtherPositionIsStillJavaSerialized() {
        final Position p = new Position(new Path(C, "x", new JSONSourcePosition("{}", 1L)));
        final byte[] bytes = KryoSingleton.write(p, false);
        assertFalse(CompactPositions.isCompact(bytes));
        // JSONSourcePosition has no equals of its own: compare what it renders
        assertEquals(p.toString(), KryoSingleton.read(bytes, false).toString());
        assertFalse(CompactPositions.isCompact(KryoSingleton.write("text", false)));
    }

    @Test
    void javaSerializedBytesOfAnOrdinalPositionStillRead() throws Exception {
        final Position p = new Position(new Path(C, "perftest", new OrdinalPosition(7)));
        final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ObjectOutputStream out = new ObjectOutputStream(bytes)) {
            out.writeObject(p);
        }
        assertEquals(p, KryoSingleton.read(bytes.toByteArray(), false));
    }
}
