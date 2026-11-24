package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.lang.reflect.Field;
import java.lang.reflect.Method;

import org.junit.jupiter.api.Test;

import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.MultiMutableStemma;
import com.webaction.recovery.MutableStemma;
import com.webaction.uuid.UUID;

/**
 * What field's restart filter (OpenProcessorCommon R17, {@code AbstractWriterApp.droppedByRestartFilter}
 * and {@code platformEpoch}) calls on the stand-ins: {@code MutableStemma.DropDuplicatesStemmaProcessor
 * .process}, {@code Stemma.getParents}, {@code MultiMutableStemma.removeMutableStemma}, and the flow's
 * {@code NodeManager.epochNumber}. Absent, a case reaching that path died with a link error (FW-123).
 */
class RestartFilterStandInTest {

    private static final UUID SOURCE = new UUID(0L, 7L);

    private static MutableStemma seenUpTo(int low, int high) {
        MutableStemma seen = new MutableStemma(SOURCE, "d");
        seen.mergeHigher(new ImmutableStemma(SOURCE, "d", new OrdinalPosition(low), new OrdinalPosition(high)));
        return seen;
    }

    private static ImmutableStemma at(int n) {
        return new ImmutableStemma(SOURCE, "d", new OrdinalPosition(n));
    }

    @Test
    void anAtStemmaHasSeenWhatItsLowMarkReaches() {
        MutableStemma seen = seenUpTo(5, 9);
        assertTrue(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(4)));
        assertTrue(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(5)), "at the mark is seen");
        assertFalse(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(6)),
                "an AT stemma compares its LOW mark, not its high one (6 < 9 but past 5)");
    }

    @Test
    void anAfterStemmaHasSeenWhatItsHighMarkReaches() {
        MutableStemma seen = new MutableStemma(SOURCE, "d") {
            @Override
            public boolean isAfter() {
                return true;
            }
        };
        seen.mergeHigher(new ImmutableStemma(SOURCE, "d", new OrdinalPosition(5), new OrdinalPosition(9)));
        assertTrue(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(9)));
        assertTrue(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(6)));
        assertFalse(MutableStemma.DropDuplicatesStemmaProcessor.process(seen, at(10)));
    }

    @Test
    void aStemmaHasNoParents() {
        assertTrue(seenUpTo(1, 1).getParents().isEmpty(), "every stemma here is a root (see Stemma)");
        assertTrue(at(1).getParents().isEmpty());
    }

    @Test
    void removeMutableStemmaDropsOnlyThatKey() {
        MultiMutableStemma filter = new MultiMutableStemma(seenUpTo(3, 3));
        String key = at(3).key();
        assertNotNull(filter.getSameMutableStemma(key));
        filter.removeMutableStemma("no-such-key");
        assertNotNull(filter.getSameMutableStemma(key));
        filter.removeMutableStemma(key);
        assertNull(filter.getSameMutableStemma(key));
        assertTrue(filter.isEmpty());
    }

    @Test
    void eachStartHasItsOwnEpochReadAsFieldReadsIt() throws Exception {
        Long first = epoch(new HarnessFlowComponent());
        Long second = epoch(new HarnessFlowComponent());
        assertNotNull(first);
        assertTrue(second > first, "a later start has a later epoch, as NodeManager.epochNumber does");
    }

    /** Field's platformEpoch(): getTopLevelFlow().getNodeManager().epochNumber, reflectively. */
    private static Long epoch(HarnessFlowComponent component) throws Exception {
        Object flow = component.getTopLevelFlow();
        Method getManager = flow.getClass().getMethod("getNodeManager");
        Object manager = getManager.invoke(flow);
        Field epochField = manager.getClass().getField("epochNumber");
        Object epoch = epochField.get(manager);
        assertEquals(Long.class, epoch.getClass());
        return (Long) epoch;
    }
}
