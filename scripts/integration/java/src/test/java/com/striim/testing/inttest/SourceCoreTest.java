package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.List;

import com.webaction.proc.events.WAEvent;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * the reader driver, exercised against reader-shaped cores.
 *
 * <p>The cores here are local rather than loaded from an op jar on purpose — what needs proving
 * is the DRIVING contract (channel proxy, tick discovery, per-tick emission, close), not
 * classloading, which {@link OperatorCore} already covers. Wiring this to a real reader jar also
 * needs constructor seam discovery: {@code resolveConstructorArgument} knows four parameter types, and a reader's
 * constructor asks for module seams it has never heard of.</p>
 */
class SourceCoreTest {

    /** The Spanner/Gong/DatabaseReader shape. */
    public interface FakeChannel {
        void emit(WAEvent event) throws Exception;

        void checkpoint(Object position);
    }

    /**
     * A channel carrying a trailing non-element argument, which the proxy must tolerate.
     *
     * <p>This was MetricsReader's shape before moving its channel onto its {@code App} and
     * put it on {@code common.Emitter<WAEvent>}. No module declares it today — the case is kept
     * because the tolerance is a property of the proxy, not of any one consumer, and deleting it
     * would leave nothing pinning that a second argument is ignored rather than rejected.</p>
     */
    public interface ChannelWithChannelArg {
        void emit(WAEvent event, int channel) throws Exception;
    }

    /** Emits a fixed script, one item per tick — the deterministic source D-D′ asked for. */
    public static final class ScriptedReader {
        /** Static, because SourceCore constructs its OWN instance — see closeReachesTheCore. */
        static int closeCount;

        private final List<List<WAEvent>> script;
        private int tickCount;

        public ScriptedReader(List<List<WAEvent>> script) {
            this.script = script;
        }

        public void tick(FakeChannel channel) throws Exception {
            List<WAEvent> batch = tickCount < script.size() ? script.get(tickCount) : List.of();
            tickCount++;
            for (WAEvent e : batch) {
                channel.emit(e);
            }
            channel.checkpoint("position-after-tick-" + tickCount);
        }

        public void close() {
            closeCount++;
        }
    }

    public static final class ChannelArgReader {
        public ChannelArgReader() {
        }

        public void tick(ChannelWithChannelArg channel) throws Exception {
            channel.emit(new WAEvent(1, null), 7);
        }
    }

    public static final class ThrowingReader {
        public ThrowingReader() {
        }

        public void tick(FakeChannel channel) throws Exception {
            throw new IllegalStateException("source unreachable");
        }
    }

    /** No tick at all — an in-stream op handed to the reader driver by mistake. */
    public static final class NotAReader {
        public NotAReader() {
        }

        public List<WAEvent> processEvent(WAEvent event) {
            return List.of();
        }
    }

    private static WAEvent event() {
        return new WAEvent(1, null);
    }

    private static SourceCore scripted(List<List<WAEvent>> script) throws Exception {
        return SourceCore.build(ScriptedReader.class, new Object[] { script }, FakeChannel.class);
    }

    @Test
    void eachTickReturnsOnlyThatTicksEmissions() throws Exception {
        WAEvent first = event();
        WAEvent second = event();
        WAEvent third = event();
        SourceCore core = scripted(List.of(List.of(first), List.of(second, third)));

        List<WAEvent> tick1 = core.processEvent(null);
        List<WAEvent> tick2 = core.processEvent(null);

        assertEquals(1, tick1.size(), "tick 1 emitted one event");
        assertSame(first, tick1.get(0));
        assertEquals(2, tick2.size(), "tick 2 emitted two — and NOT tick 1's as well");
        assertSame(second, tick2.get(0));
        assertSame(third, tick2.get(1));
    }

    @Test
    void aTickThatEmitsNothingReturnsEmptyRatherThanRepeating() throws Exception {
        SourceCore core = scripted(List.of(List.of(event())));

        core.processEvent(null);
        List<WAEvent> quiet = core.processEvent(null);

        assertTrue(quiet.isEmpty(),
                "a quiet tick must return empty; repeating the previous batch would let a case"
                        + " assert events the source never produced");
    }

    /**
     * The termination story. Ticks are supplied by the caller, so a case declaring N ticks gets
     * exactly N — no wall-clock predicate, which is what D-D′ feared would make a reader tier
     * flaky.
     */
    @Test
    void theCallerDecidesHowManyTicksHappen() throws Exception {
        List<List<WAEvent>> script = new ArrayList<List<WAEvent>>();
        for (int i = 0; i < 5; i++) {
            script.add(List.of(event()));
        }
        SourceCore core = scripted(script);

        int total = 0;
        for (int i = 0; i < 3; i++) {
            total += core.processEvent(null).size();
        }

        assertEquals(3, total, "three ticks requested, three batches drained — not the whole script");
    }

    /**
     * An earlier version of this test constructed its OWN {@code ScriptedReader}, called
     * {@code close()} on that, and asserted the flag — on an instance {@link SourceCore} never
     * touched. Gutting {@code SourceCore.close()} to {@code return;} left it green, so the close
     * path was unpinned. The count is static precisely because the core builds its own instance,
     * which is the fact the old test tripped over.
     */
    @Test
    void closeReachesTheCoreSOWNInstance() throws Exception {
        ScriptedReader.closeCount = 0;
        SourceCore core = scripted(List.of(List.of(event())));

        core.close();

        assertEquals(1, ScriptedReader.closeCount,
                "close() must reach the instance SourceCore built, not merely be callable");
    }

    @Test
    void theOutlierChannelShapeIsTolerated() throws Exception {
        SourceCore core = SourceCore.build(ChannelArgReader.class, new Object[] {},
                ChannelWithChannelArg.class);

        List<WAEvent> emitted = core.processEvent(null);

        assertEquals(1, emitted.size(),
                "emit(WAEvent, int) must still record the event; a trailing argument that is not"
                        + " an element type is ignored rather than rejected");
    }

    @Test
    void aFailingTickPropagatesTheCoresOwnException() throws Exception {
        SourceCore core = SourceCore.build(ThrowingReader.class, new Object[] {},
                FakeChannel.class);

        IllegalStateException thrown =
                assertThrows(IllegalStateException.class, () -> core.processEvent(null));

        assertEquals("source unreachable", thrown.getMessage(),
                "the core's own failure must surface, not an InvocationTargetException wrapper");
    }

    /** Non-WAEvent emissions must fail rather than vanish (Emitter<Event> permits arbitrary event types). */
    public interface EventChannel {
        void emit(com.webaction.event.Event event) throws Exception;
    }

    public static final class NonWaEventReader {
        public NonWaEventReader() {
        }

        public void tick(EventChannel channel) throws Exception {
            channel.emit(new com.webaction.event.Event() { });
        }
    }

    @Test
    void anEmissionThisHarnessCannotCollectFailsRatherThanVanishing() throws Exception {
        SourceCore core = SourceCore.build(NonWaEventReader.class, new Object[] {},
                EventChannel.class);

        IllegalStateException thrown =
                assertThrows(IllegalStateException.class, () -> core.processEvent(null));

        assertTrue(thrown.getMessage().contains("WAEvent only"),
                "silently dropping it would let a case assert 0 events and pass for the wrong"
                        + " reason: " + thrown.getMessage());
    }

    @Test
    void anInStreamOpHandedToTheReaderDriverFailsWithAUsefulMessage() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> SourceCore.build(NotAReader.class, new Object[] {}, FakeChannel.class));

        assertTrue(thrown.getMessage().contains("has no tick("),
                "the message must name what is missing: " + thrown.getMessage());
    }
}
