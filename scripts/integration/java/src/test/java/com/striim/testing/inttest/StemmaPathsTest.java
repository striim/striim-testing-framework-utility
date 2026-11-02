package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertSame;

import java.util.List;

import org.junit.jupiter.api.Test;

import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.MutableStemma;
import com.webaction.recovery.Path;
import com.webaction.uuid.UUID;

/**
 * {@code ImmutableStemma.toPaths()}, {@code Path.getPathHash()} and {@code Path.isAfter()}, which
 * field's {@code HoldAckLedger} (OpenProcessorCommon W1) reads every position through. Absent from
 * the stand-ins, every DatabaseJdbcWriter case with a recovery position died with
 * {@code NoSuchMethodError: ImmutableStemma.toPaths()}.
 */
class StemmaPathsTest {

    private static final UUID SOURCE = new UUID(0L, 1L);

    @Test
    void aStemmaIsOnePathCarryingItsOwnComponentAndMarks() {
        OrdinalPosition low = new OrdinalPosition(3);
        OrdinalPosition high = new OrdinalPosition(5);
        ImmutableStemma stemma = new ImmutableStemma(SOURCE, "d", low, high);
        List<Path> paths = stemma.toPaths();
        assertEquals(1, paths.size(), "a stemma with no parents is a root: one path, as on a server");
        Path path = paths.get(0);
        assertEquals(SOURCE, path.getComponentID());
        assertEquals("d", path.getDistributionID());
        assertSame(low, path.getLowSourcePosition());
        assertSame(high, path.getHighSourcePosition());
    }

    @Test
    void aMutableStemmaHasTheSamePaths() {
        MutableStemma acc = new MutableStemma(SOURCE, "d");
        acc.mergeHigher(new ImmutableStemma(SOURCE, "d", new OrdinalPosition(4)));
        assertEquals(acc.toImmutable().toPaths(), acc.toPaths());
    }

    @Test
    void thePathHashNamesTheComponentAndDistributionNotThePosition() {
        Path atOne = new Path(SOURCE, "d", new OrdinalPosition(1));
        Path atNine = new Path(SOURCE, "d", new OrdinalPosition(9), new OrdinalPosition(12));
        assertEquals(atOne.getPathHash(), atNine.getPathHash(),
                "HoldAckLedger groups positions on one path by this hash");
        assertNotEquals(atOne.getPathHash(), new Path(SOURCE, "e", new OrdinalPosition(1)).getPathHash());
        assertNotEquals(atOne.getPathHash(),
                new Path(new UUID(0L, 2L), "d", new OrdinalPosition(1)).getPathHash());
        assertEquals(new ImmutableStemma(SOURCE, "d", new OrdinalPosition(7)).toPaths().get(0).getPathHash(),
                atOne.getPathHash());
    }

    @Test
    void thePathHashIsThePlatformsFormula() {
        // Real: pathItems.hashCode() == itemList.toString().hashCode(), one item rendered
        // "<uuid>+<distributionID>", "*" for a null uuid or a null or empty distribution.
        assertEquals((SOURCE + "+d").hashCode(), new Path(SOURCE, "d", new OrdinalPosition(1)).getPathHash());
        assertEquals((SOURCE + "+*").hashCode(), new Path(SOURCE, null, new OrdinalPosition(1)).getPathHash());
        assertEquals((SOURCE + "+*").hashCode(), new Path(SOURCE, "", new OrdinalPosition(1)).getPathHash());
        assertEquals("*+d".hashCode(), new Path(null, "d", new OrdinalPosition(1)).getPathHash());
    }

    @Test
    void everyPathAndStemmaHereIsAt() {
        // The stand-ins take no atOrAfter marker, so all are the platform's default, AT.
        assertFalse(new Path(SOURCE, "d", new OrdinalPosition(1)).isAfter());
        ImmutableStemma stemma = new ImmutableStemma(SOURCE, "d", new OrdinalPosition(1));
        assertFalse(stemma.isAfter());
        assertFalse(stemma.toPaths().get(0).isAfter());
    }
}
