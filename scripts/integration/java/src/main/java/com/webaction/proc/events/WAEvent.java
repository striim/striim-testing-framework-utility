package com.webaction.proc.events;

import java.util.HashMap;

import com.webaction.event.Event;
import com.webaction.uuid.UUID;

/**
 * Mock of the real Striim {@code com.webaction.proc.events.WAEvent}, matching its
 * public shape on 5.4. Reproduces exactly the public
 * fields and methods ReferenceOp and {@code WAEventJsonFactory} touch:
 * {@code data}/{@code before} images, {@code metadata}/{@code userdata} maps,
 * {@code typeUUID}/{@code sourceUUID}, presence bitmaps, {@code setData}/{@code
 * setBefore} (allocate + flip the presence bit), {@code putUserdata}, and {@code
 * makeCopy} (deep copy of the images/bitmaps, fresh maps).
 *
 * <p>
 * <b>Deviation from the real class</b> (see {@link Event}'s javadoc): the real
 * {@code WAEvent} extends {@code SourceEvent} (which owns the real {@code sourceUUID}
 * field), several classes above {@code Event} in the hierarchy. Nothing in the
 * operators this harness drives references {@code SourceEvent}/{@code SimpleEvent}/
 * {@code SizedEvent} by name, so this mock collapses the chain to {@code WAEvent
 * extends Event} directly and hosts {@code sourceUUID} straight on {@code WAEvent}.
 * The real class also has no public presence-bitmap *read* helper beyond the raw
 * {@code byte[]} fields; this mock adds {@link #isDataPresent(int)}/{@link
 * #isBeforePresent(int)} so {@code WAEventJsonFactory} and the {@code BuiltInFuncs}
 * mock proxy don't need to duplicate bit arithmetic.
 */
public class WAEvent extends Event {

    public Object[] data;
    public Object[] before;
    public HashMap<String, Object> metadata = new HashMap<>();
    public HashMap<String, Object> userdata = new HashMap<>();
    public UUID typeUUID;
    public UUID sourceUUID;
    public byte[] dataPresenceBitMap;
    public byte[] beforePresenceBitMap;
    /**
     * The source's commit clock, epoch millis. On the real class this lives on
     * {@code SimpleEvent}, two supers up, and every CDC reader stamps it; a field reference in
     * the op jar resolves through the hierarchy, so declaring it here satisfies the same
     * {@code getfield}. Zero (never stamped) is what a fixture without {@code originTimeStamp}
     * yields, and is what a writer must treat as "no source time".
     */
    public long originTimeStamp;

    public WAEvent() {
    }

    public WAEvent(int columnCount, UUID sourceUUID) {
        this.data = new Object[columnCount];
        this.dataPresenceBitMap = new byte[bitmapBytes(columnCount)];
        this.sourceUUID = sourceUUID;
    }

    /** Allocates (or grows) the data image and flips the presence bit at {@code index}. */
    public void setData(int index, Object value) {
        data = ensureCapacity(data, index);
        dataPresenceBitMap = ensureBitmap(dataPresenceBitMap, index);
        data[index] = value;
        setBit(dataPresenceBitMap, index);
    }

    /** Allocates (or grows) the before image and flips the presence bit at {@code index}. */
    public void setBefore(int index, Object value) {
        before = ensureCapacity(before, index);
        beforePresenceBitMap = ensureBitmap(beforePresenceBitMap, index);
        before[index] = value;
        setBit(beforePresenceBitMap, index);
    }

    /** Clears the value and presence bit at {@code index} in the data image. */
    public void unsetData(int index) {
        if (data != null && index < data.length) {
            data[index] = null;
        }
        if (dataPresenceBitMap != null) {
            clearBit(dataPresenceBitMap, index);
        }
    }

    /** Presence read helper: whether {@code index} is set in the data image. */
    public boolean isDataPresent(int index) {
        return isBitSet(dataPresenceBitMap, index);
    }

    /** Presence read helper: whether {@code index} is set in the before image. */
    public boolean isBeforePresent(int index) {
        return isBitSet(beforePresenceBitMap, index);
    }

    public void putUserdata(String key, Object value) {
        if (userdata == null) {
            userdata = new HashMap<>();
        }
        userdata.put(key, value);
    }

    public void removeUserData(String key) {
        if (userdata != null) {
            userdata.remove(key);
        }
    }

    public void setPayload(Object[] payload) {
        this.data = payload;
    }

    public Object[] getPayload() {
        return data;
    }

    /** Deep-copies data/before images and presence bitmaps; fresh metadata/userdata maps. */
    public static WAEvent makeCopy(WAEvent src) {
        if (src == null) {
            return null;
        }
        WAEvent copy = new WAEvent();
        copy.data = src.data == null ? null : src.data.clone();
        copy.before = src.before == null ? null : src.before.clone();
        copy.dataPresenceBitMap = src.dataPresenceBitMap == null ? null : src.dataPresenceBitMap.clone();
        copy.beforePresenceBitMap = src.beforePresenceBitMap == null ? null : src.beforePresenceBitMap.clone();
        copy.metadata = src.metadata == null ? new HashMap<>() : new HashMap<>(src.metadata);
        copy.userdata = src.userdata == null ? new HashMap<>() : new HashMap<>(src.userdata);
        copy.typeUUID = src.typeUUID;
        copy.sourceUUID = src.sourceUUID;
        return copy;
    }

    private static int bitmapBytes(int columnCount) {
        return (columnCount + 7) / 8;
    }

    private static Object[] ensureCapacity(Object[] array, int index) {
        if (array == null) {
            return new Object[index + 1];
        }
        if (array.length <= index) {
            Object[] bigger = new Object[index + 1];
            System.arraycopy(array, 0, bigger, 0, array.length);
            return bigger;
        }
        return array;
    }

    private static byte[] ensureBitmap(byte[] bitmap, int index) {
        int needed = (index / 8) + 1;
        if (bitmap == null) {
            return new byte[needed];
        }
        if (bitmap.length < needed) {
            byte[] bigger = new byte[needed];
            System.arraycopy(bitmap, 0, bigger, 0, bitmap.length);
            return bigger;
        }
        return bitmap;
    }

    private static void setBit(byte[] bitmap, int index) {
        bitmap[index / 8] |= (byte) (1 << (index % 8));
    }

    private static void clearBit(byte[] bitmap, int index) {
        bitmap[index / 8] &= (byte) ~(1 << (index % 8));
    }

    private static boolean isBitSet(byte[] bitmap, int index) {
        if (bitmap == null) {
            return false;
        }
        int byteIndex = index / 8;
        if (byteIndex >= bitmap.length) {
            return false;
        }
        return (bitmap[byteIndex] & (1 << (index % 8))) != 0;
    }
}
