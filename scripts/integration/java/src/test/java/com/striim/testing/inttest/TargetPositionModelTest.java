package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.LinkedHashMap;

import org.junit.jupiter.api.Test;

import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.MultiMutableStemma;
import com.webaction.recovery.MutableStemma;
import com.webaction.recovery.Position;
import com.webaction.uuid.UUID;

/**
 * The recovery-family mocks a target's checkpoint travels through.
 *
 * <p>These are HARNESS tests, not writer tests, and the distinction is the point: a target case
 * asserts that a position written to a real database comes back unchanged, so if these mocks did
 * not round-trip, that case would fail on the harness while reading as a writer defect.</p>
 */
class TargetPositionModelTest {

    private static final UUID COMPONENT = new UUID(0L, -1L);

    private static ImmutableStemma at(long ordinal) {
        return new ImmutableStemma(COMPONENT, "d", new OrdinalPosition(ordinal));
    }

    @Test
    void mergeHigherTakesTheMaximumAndNeverLowers() {
        MultiMutableStemma committed = new MultiMutableStemma(new LinkedHashMap<>());
        committed.mergeHigher(at(5));
        committed.mergeHigher(at(2));   // out of order, as a compacted window can deliver
        assertEquals("5", rendered(committed));
    }

    @Test
    void positionRoundTripsThroughFromPosition() {
        // What a restart does: durablePosition() -> checkpoint row -> fromPosition() -> and back.
        MultiMutableStemma committed = new MultiMutableStemma(new LinkedHashMap<>());
        committed.mergeHigher(at(7));
        Position durable = committed.toPosition();
        assertEquals(durable, MultiMutableStemma.fromPosition(durable).toPosition());
    }

    @Test
    void positionEqualityIsIndependentOfPathOrder() {
        // Load-bearing: a restart case compares a position read back against one built here, and
        // map iteration order is not something a writer should be able to fail on.
        Position a = new Position(java.util.List.of(
                new com.webaction.recovery.Path(new UUID(0L, 1L), "d", new OrdinalPosition(1)),
                new com.webaction.recovery.Path(new UUID(0L, 2L), "d", new OrdinalPosition(2))));
        Position b = new Position(java.util.List.of(
                new com.webaction.recovery.Path(new UUID(0L, 2L), "d", new OrdinalPosition(2)),
                new com.webaction.recovery.Path(new UUID(0L, 1L), "d", new OrdinalPosition(1))));
        assertEquals(a, b);
    }

    @Test
    void copyConstructorIsDeepSoAFailedWindowCannotAdvanceTheCommittedPosition() {
        // App.applyBatch merges into a COPY and promotes only after the commit returns, precisely
        // so a failed apply leaves the committed value untouched. A shallow copy would share the
        // MutableStemma objects and defeat that.
        MultiMutableStemma committed = new MultiMutableStemma(new LinkedHashMap<>());
        committed.mergeHigher(at(1));
        MultiMutableStemma window = new MultiMutableStemma(committed);
        window.mergeHigher(at(9));

        assertEquals("1", rendered(committed));
        assertEquals("9", rendered(window));
        assertNotSame(committed.getMutableStemmas().values().iterator().next(),
                window.getMutableStemmas().values().iterator().next());
    }

    @Test
    void anEmptyAccumulatorHasNoPosition() {
        MultiMutableStemma committed = new MultiMutableStemma(new LinkedHashMap<>());
        assertTrue(committed.isEmpty());
        assertTrue(committed.toPosition().isEmpty());
        assertNull(ImmutableStemma.oneImmutableStemmaFromPosition(new Position()));
    }

    @Test
    void aForeignSourcePositionIsRefusedRatherThanOrderedByGuess() {
        // Nothing in this tier produces one, so this firing means the harness has a defect --
        // silently picking a winner would let a merge move the durable position arbitrarily.
        MutableStemma stemma = new MutableStemma(COMPONENT, "d");
        stemma.mergeHigher(at(1));
        assertThrows(IllegalArgumentException.class, () -> stemma.mergeHigher(
                new ImmutableStemma(COMPONENT, "d", new com.webaction.recovery.SourcePosition() {
                    @Override
                    public int compareTo(com.webaction.recovery.SourcePosition other) {
                        return 0;
                    }
                })));
    }

    private static String rendered(MultiMutableStemma committed) {
        return String.valueOf(committed.toPosition().values().iterator().next()
                .getLowSourcePosition());
    }
}
