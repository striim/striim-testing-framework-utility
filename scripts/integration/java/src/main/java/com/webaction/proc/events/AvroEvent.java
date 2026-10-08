package com.webaction.proc.events;

import java.util.HashMap;
import java.util.Map;

import org.apache.avro.generic.GenericRecord;

import com.webaction.event.Event;

/**
 * Mock of the real Striim {@code com.webaction.proc.events.AvroEvent}, matching its public
 * shape on 5.4. Reproduces the members an operator driven by this harness
 * touches: the {@link GenericRecord} payload plus the {@code metadata}/{@code userdata} maps, and
 * the {@code getData}/{@code setData}/{@code putUserdata}/{@code makeCopy} accessors around them.
 *
 * <p>This is the input type of an operator that converts a wire format rather than transforming a
 * {@link WAEvent} — AvroConverterOp is the first — and it is <b>not</b> a {@code WAEvent}. Nothing
 * here may pretend otherwise: an operator that asks {@code instanceof WAEvent} must get the same
 * answer it gets in production.
 *
 * <p><b>Deviation from the real class</b>, the same one {@link Event} records for {@code WAEvent}:
 * the real {@code AvroEvent extends SimpleEvent}, two classes above {@code Event}. Nothing in the
 * operators this harness drives references {@code SimpleEvent} by name, so the chain is collapsed
 * to {@code AvroEvent extends Event} directly. The real class also implements Kryo and Jackson
 * serialization, which nothing here needs.
 */
public class AvroEvent extends Event {

    public GenericRecord data;
    public Map<String, Object> metadata = new HashMap<>();
    public Map<String, Object> userdata = new HashMap<>();

    public AvroEvent() {
    }

    /** Matches the real class's {@code AvroEvent(long)}; the timestamp is not read by anything. */
    public AvroEvent(long timestamp) {
    }

    public GenericRecord getData() {
        return data;
    }

    public void setData(GenericRecord data) {
        this.data = data;
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

    /**
     * A copy with fresh {@code metadata}/{@code userdata} maps and the SAME {@link GenericRecord}.
     *
     * <p>The record is shared, not deep-copied, which mirrors the real class: an operator reads the
     * payload and never mutates it. The perf tier materializes one of these per record inside its
     * measured window, so a deep copy here would time an allocation production does not make.</p>
     */
    public static AvroEvent makeCopy(AvroEvent src) {
        if (src == null) {
            return null;
        }
        AvroEvent copy = new AvroEvent();
        copy.data = src.data;
        copy.metadata = src.metadata == null ? new HashMap<>() : new HashMap<>(src.metadata);
        copy.userdata = src.userdata == null ? new HashMap<>() : new HashMap<>(src.userdata);
        return copy;
    }

    @Override
    public String toString() {
        return "AvroEvent{data=" + data + ", metadata=" + metadata + ", userdata=" + userdata + "}";
    }
}
