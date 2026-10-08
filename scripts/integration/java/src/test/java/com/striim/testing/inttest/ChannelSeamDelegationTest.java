package com.striim.testing.inttest;

import java.net.URL;
import java.net.URLClassLoader;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * the harness answers a reader channel method it does not implement by asking the op.
 *
 * <p><b>Why this capability exists at all.</b> {@link SourceCore}'s channel proxy answers
 * {@code emit} and {@code checkpoint} and used to throw on everything else, asserting that "a
 * reader core should only emit and checkpoint through its channel". <b>That was false for four of
 * the five readers.</b> The first reader — the pathfinder, and for a while the only one with
 * an integration tier — reaches its platform state through a CONSTRUCTOR seam, which
 * {@code IntegrationSeams.seamFor} already covered. A change-data reader reaches it through the
 * channel and could not build a tier at all: {@code tick}'s first statement resolves a checkpoint
 * file from {@code checkpointDir()}, {@code appQualifiedName()} and
 * {@code componentQualifiedName()}. An API-polling reader reaches the channel the same way, though it needs
 * no delegation of its own: its seam declares nothing beyond {@code emit} and {@code checkpoint},
 * which the proxy answers natively.</p>
 *
 * <p><b>These are tests for shipped test-support code</b>, which this programme learned to insist
 * on the hard way — an exception-store reader's scripted store shipped a paging bug, and the DatabaseReader
 * seams shipped a swallowed parse error, both in fixtures no test covered.</p>
 */
class ChannelSeamDelegationTest {

    private static final String GOOD = "com.striim.testing.inttest.seamfixtures.good";
    private static final String WRONG_SHAPE = "com.striim.testing.inttest.seamfixtures.wrongshape";
    private static final String ABSENT = "com.striim.testing.inttest.seamfixtures.nosuchpackage";

    private static URLClassLoader thisLoader() {
        return new URLClassLoader(new URL[0], ChannelSeamDelegationTest.class.getClassLoader());
    }

    private static Map<String, Object> props() {
        Map<String, Object> p = new LinkedHashMap<String, Object>();
        p.put("IntegrationAppQualifiedName", "admin.MyApp");
        p.put("IntegrationComponentQualifiedName", "admin.MySource");
        return p;
    }

    private static Object call(String pkg, String method, Class<?> returnType) {
        return IntegrationSeamsLookup.channelValueFor(
                pkg, "com.example.SourceChannel", method, returnType, props(), thisLoader());
    }

    // ---------------------------------------------------------------------
    // the happy path — the two calls that actually blocked a change-data reader
    // ---------------------------------------------------------------------

    @Test
    void answersTwoChannelMethodsThatShareAReturnType() {
        // ⚠ The reason this convention keys on the METHOD NAME rather than the type, as seamFor
        // does: both of these return String, so a type key could not tell them apart.
        assertEquals("admin.MyApp", call(GOOD, "appQualifiedName", String.class));
        assertEquals("admin.MySource", call(GOOD, "componentQualifiedName", String.class));
    }

    @Test
    void aReferenceReturnMayLegitimatelyBeNull() {
        // "The platform cannot answer yet" is a real state these accessors model, and a case may
        // want to drive it deliberately. null must not be mistaken for a missing seam.
        assertNull(call(GOOD, "deferredName", String.class));
    }

    @Test
    void aPrimitiveReturnArrivesBoxedAndIsAccepted() {
        assertEquals(42, call(GOOD, "pageSize", int.class));
    }

    // ---------------------------------------------------------------------
    // the failures, each naming what the author must do
    // ---------------------------------------------------------------------

    @Test
    void anOpWithNoIntegrationSeamsIsToldToShipOne() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> call(ABSENT, "appQualifiedName", String.class));
        assertTrue(e.getMessage().contains("channelValueFor"), e.getMessage());
        // The message must name the METHOD that could not be answered, not just the convention --
        // an author reading it needs to know which call to implement.
        assertTrue(e.getMessage().contains("appQualifiedName"), e.getMessage());
    }

    @Test
    void anOpWithSeamsButNoChannelMethodIsToldWhySeamForCannotServe() {
        // wrongshape ships an IntegrationSeams with neither a valid seamFor nor a channelValueFor.
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> call(WRONG_SHAPE, "appQualifiedName", String.class));
        assertTrue(e.getMessage().contains("channelValueFor"), e.getMessage());
        assertTrue(e.getMessage().contains("keys on a TYPE"), e.getMessage());
    }

    @Test
    void anUndeclaredChannelMethodThrowsRatherThanReturningAPlausibleDefault() {
        // ⚠ The load-bearing one. A harness that invented a default here -- an empty app name, a
        // temp dir -- would let a reader's checkpoint identity degrade to a node-wide shared
        // filename while the tier stayed green. The op declares the answer or the case fails.
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> call(GOOD, "somethingNobodyDeclared", String.class));
        assertTrue(e.getMessage().contains("threw while answering"), e.getMessage());
    }

    @Test
    void anAnswerOfTheWrongTypeIsRejectedRatherThanClassCastingLater() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> call(GOOD, "appQualifiedName", Integer.class));
        assertTrue(e.getMessage().contains("answered a java.lang.String"), e.getMessage());
    }

    @Test
    void nullForAPrimitiveReturnIsRejectedWithItsOwnMessage() {
        // Distinct from the reference case above: unboxing null would NPE inside the proxy, far
        // from the seam that caused it.
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> call(GOOD, "deferredName", int.class));
        assertTrue(e.getMessage().contains("primitive"), e.getMessage());
    }

    // ---------------------------------------------------------------------
    // ⚠ THE DELEGATION ITSELF — which this file's name promised and did not test
    // ---------------------------------------------------------------------
    //
    // Every test above calls IntegrationSeamsLookup directly. A review severed the actual wire --
    // replacing SourceCore's proxy default branch with `return null` -- and all 117 harness tests
    // stayed green. The lookup was covered; the thing that CALLS it was not.

    /** A reader channel with more than emit/checkpoint, which is what forced this feature. */
    public interface WideChannel {
        void emit(WAEvent event) throws Exception;
        void checkpoint(Object position);
        String appQualifiedName();
        java.io.File checkpointDir();
    }

    /** A core the harness can drive: one tick, one event, and it reads the channel first. */
    public static final class WideReader {
        public static String sawAppName;

        public void tick(WideChannel channel) throws Exception {
            sawAppName = channel.appQualifiedName();
            WAEvent event = new WAEvent(1, null);
            event.metadata = new java.util.HashMap<>();
            event.userdata = new java.util.HashMap<>();
            event.data = new Object[] { sawAppName };
            channel.emit(event);
        }
    }

    private static SourceCore wideCore(SourceCore.ChannelSeams seams) throws Exception {
        return SourceCore.of(new WideReader(), WideReader.class, WideChannel.class, seams);
    }

    @Test
    void anUnknownChannelMethodIsDELEGATEDToTheOpRatherThanAnsweredLocally() throws Exception {
        // The wire itself: sever it and the core sees null instead of the op's answer.
        SourceCore core = wideCore((method, returnType) ->
                "appQualifiedName".equals(method) ? "admin.FromTheSeam" : null);
        WideReader.sawAppName = null;

        core.processEvent(new WAEvent());

        assertEquals("admin.FromTheSeam", WideReader.sawAppName,
                "the channel method must reach the op's seam, not be answered by the harness");
    }

    @Test
    void emitAndCheckpointAreHandledLOCALLYAndNeverDelegated() throws Exception {
        // ⚠ The other half of the contract. If emit were delegated, the harness would stop
        // collecting events and every reader case would silently assert nothing.
        List<String> delegated = new ArrayList<String>();
        SourceCore core = wideCore((method, returnType) -> {
            delegated.add(method);
            return "admin.FromTheSeam";
        });

        List<WAEvent> emitted = core.processEvent(new WAEvent());

        assertEquals(1, emitted.size(), "emit must still be collected by the harness");
        assertTrue(!delegated.contains("emit"), "emit must not be delegated: " + delegated);
        assertTrue(!delegated.contains("checkpoint"), "checkpoint must not be delegated: " + delegated);
        assertTrue(delegated.contains("appQualifiedName"), "the unknown one must be: " + delegated);
    }

    @Test
    void withNoOpToAskAnUnknownMethodStillThrows() throws Exception {
        // The unit-test entry point has no jar to delegate to; it must say so rather than invent.
        SourceCore core = wideCore(null);
        UnsupportedOperationException e = assertThrows(UnsupportedOperationException.class,
                () -> core.processEvent(new WAEvent()));
        assertTrue(e.getMessage().contains("appQualifiedName"), e.getMessage());
    }

    /** A reader channel that also receives positioned emissions (recovery design r2). */
    public interface PositionedChannel {
        void emit(WAEvent event) throws Exception;
        void emitAt(WAEvent event, Object coordinate) throws Exception;
    }

    /** Emits one plain and one positioned event, in that order, so order is observable. */
    public static final class PositionedReader {
        public void tick(PositionedChannel channel) throws Exception {
            WAEvent first = new WAEvent(1, null);
            first.metadata = new java.util.HashMap<>();
            first.userdata = new java.util.HashMap<>();
            first.data = new Object[] { "plain" };
            channel.emit(first);
            WAEvent second = new WAEvent(1, null);
            second.metadata = new java.util.HashMap<>();
            second.userdata = new java.util.HashMap<>();
            second.data = new Object[] { "positioned" };
            channel.emitAt(second, new Object());
        }
    }

    @Test
    void emitAtIsCollectedLikeEmit() throws Exception {
        // The proxy wire for positioned emission: without the emitAt case the channel proxy
        // would delegate (or fail), and the harness would lose the event. The coordinate is
        // deliberately not collected — this tier has no restart, so persistence is not
        // observable here.
        SourceCore core = SourceCore.of(new PositionedReader(), PositionedReader.class,
                PositionedChannel.class, (method, returnType) -> {
                    throw new AssertionError("unexpected delegation: " + method);
                });

        List<WAEvent> emitted = core.processEvent(new WAEvent());

        assertEquals(2, emitted.size(), "both emit and emitAt must be collected");
        assertEquals("plain", emitted.get(0).data[0]);
        assertEquals("positioned", emitted.get(1).data[0]);
    }
}
