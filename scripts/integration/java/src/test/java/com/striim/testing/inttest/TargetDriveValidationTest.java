package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.webaction.proc.events.WAEvent;

/**
 * {@link IntegrationProcessor#driveTarget}'s guards, and the manifest-independent half of the
 * target contract. The drive itself needs a real database and lives in the regression cases.
 */
class TargetDriveValidationTest {

    private static List<WAEvent> events(int count) {
        List<WAEvent> out = new ArrayList<>();
        for (int i = 0; i < count; i++) {
            out.add(new WAEvent());
        }
        return out;
    }

    private static TargetSpec restartingAfter(int... points) {
        TargetSpec spec = new TargetSpec();
        List<Integer> list = new ArrayList<>();
        for (int p : points) {
            list.add(p);
        }
        spec.restartAfter = list;
        return spec;
    }

    @Test
    void restartAtOrAboveTheEventCountIsRefused() {
        // The restart would happen after the last event, so nothing would replay and the case
        // would report green having tested nothing.
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.driveTarget(new File("unused.jar"), Map.of(),
                        events(3), null, null, restartingAfter(3)));
        assertTrue(e.getMessage().contains("no event would replay"), e.getMessage());
    }

    @Test
    void restartBelowOneIsRefused() {
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.driveTarget(new File("unused.jar"), Map.of(),
                        events(3), null, null, restartingAfter(0)));
        assertTrue(e.getMessage().contains("at least 1"), e.getMessage());
    }

    @Test
    void everyPointInAListIsValidated() {
        // The list form must not weaken the checks: an out-of-range point is refused wherever it
        // sits, not only when it is the only one.
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.driveTarget(new File("unused.jar"), Map.of(),
                        events(5), null, null, restartingAfter(2, 5)));
        assertTrue(e.getMessage().contains("no event would replay"), e.getMessage());
    }

    @Test
    void positionsDefaultToOnAndAreExplicitlyDisableable() {
        TargetSpec spec = new TargetSpec();
        assertTrue(spec.withPositions(), "a target case carries positions unless it opts out");
        spec.positions = Boolean.FALSE;
        assertEquals(false, spec.withPositions());
    }

    @Test
    void aRecordingCallbackSeparatesTheCountFromThePositions() {
        // §43.24a's shape: the console's "Total output" needs the COUNT, and the position path
        // is never taken under no-recovery. A case has to be able to tell those apart.
        RecordingReceiptCallback receipts = new RecordingReceiptCallback();
        receipts.ack(3);
        assertEquals(3, receipts.events());
        assertTrue(receipts.positions().isEmpty());

        receipts.ack(1, new com.webaction.recovery.ImmutableStemma(
                new com.webaction.uuid.UUID(0L, -1L), "d", new OrdinalPosition(9)));
        assertEquals(4, receipts.events());
        assertEquals(1, receipts.positions().size());
    }
}
