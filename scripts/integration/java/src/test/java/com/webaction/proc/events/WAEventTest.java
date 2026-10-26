package com.webaction.proc.events;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.util.HashMap;

import org.junit.jupiter.api.Test;

/** The mock must behave as the real 5.4.0.6C class (javap of Common-5.4.0.6C.jar). */
class WAEventTest {

    @Test
    void constructorAllocatesOnlyTheBitmaps() {
        WAEvent e = new WAEvent(9, null);
        assertNull(e.data);
        assertNull(e.before);
        assertNull(e.metadata);
        assertNull(e.userdata);
        assertEquals(2, e.dataPresenceBitMap.length);
        assertEquals(2, e.beforePresenceBitMap.length);
        assertEquals(1, new WAEvent(0, null).dataPresenceBitMap.length);
        assertEquals(2, new WAEvent(7, null).dataPresenceBitMap.length);
    }

    @Test
    void setDataAndSetBeforeNeverAllocate() {
        WAEvent e = new WAEvent(3, null);
        assertThrows(NullPointerException.class, () -> e.setData(0, "x"));
        assertThrows(NullPointerException.class, () -> e.setBefore(0, "x"));
        assertThrows(NullPointerException.class, () -> e.unsetData(0));
        e.data = new Object[3];
        assertThrows(ArrayIndexOutOfBoundsException.class, () -> e.setData(3, "x"));
        assertEquals(3, e.data.length);
    }

    @Test
    void presenceBitmapHoldsSevenColumnsPerByte() {
        WAEvent e = new WAEvent(9, null);
        e.data = new Object[9];
        for (int i = 0; i < 9; i++) {
            if (i != 7) {
                e.setData(i, "v");
            }
        }
        assertArrayEquals(new byte[] { 0x7f, 0x02 }, e.dataPresenceBitMap);
        e.unsetData(8);
        assertArrayEquals(new byte[] { 0x7f, 0x00 }, e.dataPresenceBitMap);
    }

    @Test
    void getPayloadIsDataAndMetadata() {
        WAEvent e = new WAEvent();
        e.data = new Object[] { "a" };
        e.metadata = new HashMap<>();
        Object[] payload = e.getPayload();
        assertEquals(2, payload.length);
        assertSame(e.data, payload[0]);
        assertSame(e.metadata, payload[1]);
    }

    @Test
    void makeCopyKeepsNullMapsNullAndSizesBitmapsFromData() {
        WAEvent src = new WAEvent();
        src.dataPresenceBitMap = new byte[] { 1 };
        WAEvent copy = WAEvent.makeCopy(src);
        assertNull(copy.data);
        assertNull(copy.metadata);
        assertNull(copy.userdata);
        assertArrayEquals(new byte[] { 0 }, copy.dataPresenceBitMap);
        assertNull(WAEvent.makeCopy(null));
    }
}
