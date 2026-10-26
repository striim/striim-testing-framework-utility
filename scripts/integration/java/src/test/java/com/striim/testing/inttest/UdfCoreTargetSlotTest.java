package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.File;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.Test;

/** WAEvent.setData never grows the image, so a jsonnode target slot must name an existing column. */
class UdfCoreTargetSlotTest {

    public static final class Fixture {
        private Fixture() {
        }

        public static JsonNode same(JsonNode node) {
            return node;
        }
    }

    private static UdfCore core(String target) throws Exception {
        UdfSpec spec = new UdfSpec();
        spec.className = Fixture.class.getName();
        spec.kind = "jsonnode";
        spec.source = "data[0]";
        spec.target = target;
        UdfSpec.Step step = new UdfSpec.Step();
        step.function = "same";
        step.args = List.of(Map.of("reg", true));
        spec.pipeline = List.of(step);
        return UdfCore.build(new File("absent.jar"), spec, Map.of());
    }

    private static WAEvent twoColumns() {
        WAEvent e = new WAEvent(2, null);
        e.data = new Object[2];
        e.setData(0, "{\"a\":1}");
        return e;
    }

    @Test
    void writesAnExistingColumn() throws Exception {
        WAEvent out = core("data[1]").processEvent(twoColumns()).get(0);
        assertEquals("{\"a\":1}", out.data[1]);
        assertTrue(out.isDataPresent(1));
    }

    @Test
    void refusesASlotPastTheImage() throws Exception {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> core("data[2]").processEvent(twoColumns()));
        assertTrue(e.getMessage().contains("data[2] is outside the event's 2-column image"), e.getMessage());
    }
}
