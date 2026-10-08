package com.webaction.ser;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.ObjectInputStream;
import java.io.ObjectOutputStream;

/**
 * Mock of the platform's {@code com.webaction.ser.KryoSingleton} — how a writer encodes the
 * {@code Position} it stamps into its checkpoint table.
 *
 * <h2>What this does NOT claim</h2>
 * <b>The bytes are not the platform's.</b> This mock round-trips through plain Java serialization;
 * the platform uses Kryo with a registered class table. The two are not interchangeable, so this
 * tier CANNOT certify the property {@code KryoPositionCodec}'s javadoc says is the reason it exists
 * — that a user migrating from the built-in {@code DatabaseWriter} can point this writer at their
 * existing checkpoint table and have the position already in it read correctly. <b>That is a T4
 * assertion against a real Striim, and nothing here substitutes for it.</b>
 *
 * <p>What this mock does certify is the property T2 is for: a position written to a real database
 * and read back out of it after a restart is the position that was written. That is the writer's
 * own round trip, and it is independent of the encoding.</p>
 */
public class KryoSingleton {

    private KryoSingleton() {
    }

    /**
     * Serializes {@code value}.
     *
     * @param value            the object to encode; must be {@code Serializable}
     * @param registeredOnly   ignored — the platform's class-registration table has no analogue here
     * @return the encoded bytes
     */
    public static byte[] write(Object value, boolean registeredOnly) {
        try {
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (ObjectOutputStream out = new ObjectOutputStream(bytes)) {
                out.writeObject(value);
            }
            return bytes.toByteArray();
        } catch (Exception e) {
            throw new IllegalStateException("inttest KryoSingleton mock could not encode "
                    + (value == null ? "null" : value.getClass().getName()), e);
        }
    }

    /**
     * Deserializes what {@link #write} produced.
     *
     * @param bytes          the encoded bytes
     * @param registeredOnly ignored, as in {@link #write}
     * @return the decoded object
     */
    public static Object read(byte[] bytes, boolean registeredOnly) {
        try (ObjectInputStream in = new ObjectInputStream(new ByteArrayInputStream(bytes))) {
            return in.readObject();
        } catch (Exception e) {
            throw new IllegalStateException("inttest KryoSingleton mock could not decode "
                    + (bytes == null ? "null" : bytes.length + " byte(s)") + ". If these bytes came"
                    + " from a real Striim, that is expected: this mock is NOT byte-compatible with"
                    + " the platform's Kryo encoding -- see its javadoc.", e);
        }
    }
}
