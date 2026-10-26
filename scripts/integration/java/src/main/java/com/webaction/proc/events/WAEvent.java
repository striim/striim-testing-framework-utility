package com.webaction.proc.events;

import java.util.HashMap;

import com.webaction.event.Event;
import com.webaction.uuid.UUID;

/**
 * Mock of the real Striim {@code com.webaction.proc.events.WAEvent}. Every method the real
 * class declares here behaves as the 5.4.0.6C bytecode does: the constructors allocate only
 * the presence bitmaps ({@code n/7+1} bytes, 7 columns per byte), never {@code data}/{@code
 * before} or the maps; {@code setData}/{@code setBefore}/{@code unsetData} never grow, so a
 * missing or short image throws NPE/AIOOBE as in production; {@code getPayload} returns
 * {@code {data, metadata}}; {@code makeCopy} copies what the real one copies.
 *
 * <p>
 * Deviations: the hierarchy is collapsed to {@code WAEvent extends Event} (see {@link Event}),
 * so {@code sourceUUID} and {@code originTimeStamp} live here; {@link #isDataPresent}/{@link
 * #isBeforePresent} are harness-only read helpers (an op compiled against the real jar cannot
 * call them); methods the harness never needs (Kryo, JSON, size accounting) are absent.
 */
public class WAEvent extends Event {

    public Object[] data;
    public HashMap<String, Object> metadata;
    public HashMap<String, Object> userdata;
    public Object[] before;
    public byte[] dataPresenceBitMap;
    public byte[] beforePresenceBitMap;
    public UUID typeUUID;
    public UUID sourceUUID;
    /** The source's commit clock, epoch millis; {@code SimpleEvent} on the real class. Zero means none. */
    public long originTimeStamp;

    public WAEvent() {
    }

    public WAEvent(int columnCount, UUID sourceUUID) {
        this.sourceUUID = sourceUUID;
        int bytes = columnCount / 7 + 1;
        this.dataPresenceBitMap = new byte[bytes];
        this.beforePresenceBitMap = new byte[bytes];
    }

    public void setData(int index, Object value) {
        data[index] = value;
        int byteIndex = index / 7;
        byte mask = (byte) (1 << (index % 7));
        dataPresenceBitMap[byteIndex] = (byte) (dataPresenceBitMap[byteIndex] | mask);
    }

    public void unsetData(int index) {
        data[index] = null;
        int byteIndex = index / 7;
        byte mask = (byte) ~(1 << (index % 7));
        dataPresenceBitMap[byteIndex] = (byte) (dataPresenceBitMap[byteIndex] & mask);
    }

    public void setBefore(int index, Object value) {
        before[index] = value;
        int byteIndex = index / 7;
        byte mask = (byte) (1 << (index % 7));
        beforePresenceBitMap[byteIndex] = (byte) (beforePresenceBitMap[byteIndex] | mask);
    }

    /** Harness-only: whether {@code index} is set in the data bitmap; false when out of range. */
    public boolean isDataPresent(int index) {
        return isBitSet(dataPresenceBitMap, index);
    }

    /** Harness-only: whether {@code index} is set in the before bitmap; false when out of range. */
    public boolean isBeforePresent(int index) {
        return isBitSet(beforePresenceBitMap, index);
    }

    public synchronized void putUserdata(String key, Object value) {
        if (userdata == null) {
            userdata = new HashMap<>();
        }
        userdata.put(key, value);
    }

    public synchronized void removeUserData(String key) {
        if (userdata != null) {
            userdata.remove(key);
        }
    }

    public void setPayload(Object[] payload) {
        this.data = payload;
    }

    public Object[] getPayload() {
        return new Object[] { data, metadata };
    }

    public static WAEvent makeCopy(WAEvent src) {
        if (src == null) {
            return src;
        }
        int columns = src.data != null ? src.data.length : 0;
        WAEvent copy = new WAEvent(columns, src.sourceUUID);
        copy.typeUUID = src.typeUUID;
        if (src.data != null) {
            copy.data = src.data.clone();
            if (src.dataPresenceBitMap != null) {
                copy.dataPresenceBitMap = src.dataPresenceBitMap.clone();
            }
        }
        if (src.before != null) {
            copy.before = src.before.clone();
            if (src.beforePresenceBitMap != null) {
                copy.beforePresenceBitMap = src.beforePresenceBitMap.clone();
            }
        }
        if (src.metadata != null) {
            copy.metadata = new HashMap<>(src.metadata);
        }
        if (src.userdata != null) {
            copy.userdata = new HashMap<>(src.userdata);
        }
        return copy;
    }

    private static boolean isBitSet(byte[] bitmap, int index) {
        if (bitmap == null || index < 0 || index / 7 >= bitmap.length) {
            return false;
        }
        return (bitmap[index / 7] & (1 << (index % 7))) != 0;
    }
}
