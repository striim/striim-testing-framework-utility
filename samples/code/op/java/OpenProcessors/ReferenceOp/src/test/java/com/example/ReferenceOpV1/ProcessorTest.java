package com.example.ReferenceOpV1;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.example.common.BuiltInFuncs;
import com.example.common.Logger;
import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

class ProcessorTest {

    private static final Logger QUIET = new Logger("test", false, false);

    private static WAEvent event(int cols) {
        WAEvent e = new WAEvent(cols, UUID.genCurTimeUUID());
        e.data = new Object[cols];
        return e;
    }

    private static Processor processor(boolean enableInspection, BuiltInFuncs funcs) {
        Map<String, Object> props = new HashMap<>();
        props.put("EnableInspection", Boolean.toString(enableInspection));
        return new Processor(props, funcs, QUIET);
    }

    private static Processor processor(boolean enableInspection) {
        return processor(enableInspection, mock(BuiltInFuncs.class));
    }

    @Test
    void nullEventReturnsEmptyList() {
        Processor p = processor(false);

        List<WAEvent> result = p.processEvent(null);

        assertTrue(result.isEmpty());
    }

    @Test
    void outputIsDistinctInstanceFromInput() {
        Processor p = processor(false);
        WAEvent e = event(2);
        e.data[0] = "a";
        e.data[1] = "b";

        List<WAEvent> result = p.processEvent(e);

        assertEquals(1, result.size());
        assertNotSame(e, result.get(0));
    }

    @Test
    void copyPreservesDataArrayValues() {
        Processor p = processor(false);
        WAEvent e = event(3);
        e.data[0] = "x";
        e.data[1] = 42;
        e.data[2] = null;

        WAEvent copy = p.processEvent(e).get(0);

        assertArrayEquals(new Object[] { "x", 42, null }, copy.data);
        assertNotSame(e.data, copy.data);
    }

    @Test
    void copyPreservesBeforeImage() {
        Processor p = processor(false);
        WAEvent e = event(2);
        e.data[0] = "new1";
        e.data[1] = "new2";
        e.before = new Object[] { "old1", "old2" };

        WAEvent copy = p.processEvent(e).get(0);

        assertArrayEquals(new Object[] { "old1", "old2" }, copy.before);
        assertNotSame(e.before, copy.before);
    }

    @Test
    void copyPreservesMetadata() {
        Processor p = processor(false);
        WAEvent e = event(1);
        e.data[0] = "v";
        e.metadata = new HashMap<>();
        e.metadata.put("TableName", "T1");

        WAEvent copy = p.processEvent(e).get(0);

        assertEquals("T1", copy.metadata.get("TableName"));
        assertNotSame(e.metadata, copy.metadata);
    }

    @Test
    void copyPreservesPresenceBitmap() {
        Processor p = processor(false);
        WAEvent e = event(2);
        e.data[0] = "a";
        e.data[1] = "b";
        // {1, 0} rather than a uniform value: the copy must carry the bitmap through, and a
        // bitmap of all-same bytes would pass even if the copier substituted a fresh one.
        e.dataPresenceBitMap = new byte[] { 1, 0 };

        WAEvent copy = p.processEvent(e).get(0);

        assertArrayEquals(new byte[] { 1, 0 }, copy.dataPresenceBitMap);
        assertNotSame(e.dataPresenceBitMap, copy.dataPresenceBitMap);
    }

    @Test
    void addsProcessedTrueToUserdata() {
        Processor p = processor(false);
        WAEvent e = event(1);
        e.data[0] = "v";

        WAEvent copy = p.processEvent(e).get(0);

        assertEquals("true", copy.userdata.get("processed"));
    }

    @Test
    void doesNotMutateSourceEventUserdata() {
        Processor p = processor(false);
        WAEvent e = event(1);
        e.data[0] = "v";

        p.processEvent(e);

        // null, not "empty": the source's userdata map must never be created either. An
        // implementation that copied after touching e.userdata would leave an empty map here.
        assertEquals(null, e.userdata);
    }

    @Test
    void preservesExistingUserdataAlongsideProcessedFlag() {
        Processor p = processor(false);
        WAEvent e = event(1);
        e.data[0] = "v";
        e.putUserdata("origin", "sourceA");

        WAEvent copy = p.processEvent(e).get(0);

        assertEquals("sourceA", copy.userdata.get("origin"));
        assertEquals("true", copy.userdata.get("processed"));
    }

    @Test
    void inspectionDisabledNeverConsultsResolver() {
        BuiltInFuncs resolver = mock(BuiltInFuncs.class);
        Processor p = processor(false, resolver);
        WAEvent e = event(3);
        e.data[0] = "a";
        e.data[1] = "b";
        e.data[2] = "c";

        p.processEvent(e);

        verifyNoInteractions(resolver);
    }

    @Test
    void inspectionEnabledConsultsResolverForEveryColumn() {
        BuiltInFuncs resolver = mock(BuiltInFuncs.class);
        when(resolver.IS_PRESENT(any(WAEvent.class), any(Object[].class), anyInt())).thenReturn(true);
        Processor p = processor(true, resolver);
        WAEvent e = event(3);
        e.data[0] = "a";
        e.data[1] = "b";
        e.data[2] = "c";

        List<WAEvent> result = p.processEvent(e);

        verify(resolver, times(3)).IS_PRESENT(any(WAEvent.class), any(Object[].class), anyInt());

        assertEquals(1, result.size());
        assertArrayEquals(new Object[] { "a", "b", "c" }, result.get(0).data);
        assertEquals("true", result.get(0).userdata.get("processed"));
        assertEquals(Boolean.TRUE, result.get(0).userdata.get("column 0"));
        assertEquals(Boolean.TRUE, result.get(0).userdata.get("column 1"));
        assertEquals(Boolean.TRUE, result.get(0).userdata.get("column 2"));
    }

    @Test
    void inspectionDisabledWritesNoColumnUserdata() {
        Processor p = processor(false);
        WAEvent e = event(2);
        e.data[0] = "a";
        e.data[1] = "b";

        WAEvent copy = p.processEvent(e).get(0);

        assertEquals("true", copy.userdata.get("processed"));
        assertNull(copy.userdata.get("column 0"));
        assertNull(copy.userdata.get("column 1"));
    }

    @Test
    void inspectionNeverMutatesTheSourceEvent() {
        BuiltInFuncs resolver = mock(BuiltInFuncs.class);
        when(resolver.IS_PRESENT(any(WAEvent.class), any(Object[].class), anyInt())).thenReturn(true);
        Processor p = processor(true, resolver);
        WAEvent e = event(2);
        e.data[0] = "a";
        e.data[1] = "b";

        WAEvent copy = p.processEvent(e).get(0);

        assertEquals(Boolean.TRUE, copy.userdata.get("column 0"));
        assertNull(e.userdata == null ? null : e.userdata.get("column 0"));
    }

    @Test
    void inspectionEnabledWithScriptedPresencePerColumn() {
        BuiltInFuncs resolver = mock(BuiltInFuncs.class);

        // Alternating by index is the assertion: a stub returning one constant cannot tell
        // "asked per column and recorded each answer" apart from "asked once and reused it".
        when(resolver.IS_PRESENT(any(WAEvent.class), any(Object[].class), anyInt()))
                .thenAnswer(inv -> (int) inv.getArgument(2) % 2 == 0);
        Processor p = processor(true, resolver);
        WAEvent e = event(4);
        e.data[0] = "a";
        e.data[1] = "b";
        e.data[2] = "c";
        e.data[3] = "d";

        WAEvent copy = p.processEvent(e).get(0);

        verify(resolver).IS_PRESENT(e, e.data, 0);
        verify(resolver).IS_PRESENT(e, e.data, 1);
        verify(resolver).IS_PRESENT(e, e.data, 2);
        verify(resolver).IS_PRESENT(e, e.data, 3);
        assertArrayEquals(new Object[] { "a", "b", "c", "d" }, copy.data);

        assertEquals(Boolean.TRUE, copy.userdata.get("column 0"));
        assertEquals(Boolean.FALSE, copy.userdata.get("column 1"));
        assertEquals(Boolean.TRUE, copy.userdata.get("column 2"));
        assertEquals(Boolean.FALSE, copy.userdata.get("column 3"));
    }

    @Test
    void closeIsNoOp() {
        Processor p = processor(false);
        p.close();
    }
}
