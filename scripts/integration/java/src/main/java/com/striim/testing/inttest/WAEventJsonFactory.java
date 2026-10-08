package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.function.IntPredicate;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.webaction.event.Event;
import com.webaction.proc.events.AvroEvent;
import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;
import org.joda.time.DateTime;

/**
 * Reader/writer for the compact WAEvent JSON fixture format (docs/INTEGRATION-TESTS.md):
 *
 * <pre>
 * {
 *   "kind":     "waevent",                          // optional, defaults to "waevent"
 *   "metadata": { "TableName": "CUSTOMER", ... },    // required
 *   "before":   { "values": [...], "present": [...] }, // optional (omit for INSERTs)
 *   "data":     { "values": [...], "present": [...] }, // optional
 *   "userdata": { ... },                             // optional, defaults to {}
 *   "originTimeStamp": 1757900000000,                // optional, the source commit time in epoch millis
 *   "source": "reader-a"                             // optional, names the publishing source (WAEvent.sourceUUID)
 * }
 * </pre>
 *
 * <p>
 * A present slot is applied via {@link WAEvent#setData} / {@link WAEvent#setBefore} so
 * the presence bitmap is authoritative. An absent slot keeps its presence bit clear; a non-null
 * value written there is stored in the array directly, without setting the bit, as a CDC source
 * such as a GoldenGate trail leaves one. That is what lets a fixture prove an operator ignores an
 * absent slot's value; an absent slot written {@code null} stays {@code null}. The writer
 * inverts this by deriving {@code present[]} from {@link WAEvent#isDataPresent}/
 * {@link WAEvent#isBeforePresent} — never from {@code value != null}.
 *
 * <p>
 * <b>A value is any JSON scalar, or {@code {"$hex": "00ff"}} for a {@code byte[]}</b> — JSON has
 * no byte type, so binary would otherwise be inexpressible rather than merely untested. It reads
 * and writes symmetrically; see {@link #decodeHex} for why it must be a reserved-key object
 * rather than a bare string.
 *
 * <p>
 * {@code typeUUID} is not read from or written to the fixture; the reader stamps a
 * fresh {@link UUID#genCurTimeUUID()} on every event.
 *
 * <p>
 * <b>The {@code kind} discriminator has two INPUT branches.</b> {@code "waevent"} is this class;
 * {@code "avro"} is {@link AvroEventJsonFactory}, for an operator whose input is an
 * {@code AvroEvent} rather than a {@code WAEvent} (AvroConverterOp). Read input through
 * {@link #readInputEvents}, which dispatches; {@link #readEvents} stays {@code WAEvent}-only and
 * is what every {@code WAEvent}-shaped caller — expected fixtures, target cases — keeps using. A
 * future {@code "jsonnode"} branch adds a third the same way.
 *
 * <p>
 * <b>Output is {@code WAEvent} only</b>, and there is no avro writer: an operator taking an
 * {@code AvroEvent} emits {@code WAEvent}s, so nothing serializes one back.
 */
public final class WAEventJsonFactory {

    private static final String KIND_WAEVENT = "waevent";
    private static final String KIND_AVRO = "avro";

    /** Reserved key of the object form that carries a byte array; see {@link #toJavaValue}. */
    private static final String HEX_KEY = "$hex";

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private WAEventJsonFactory() {
    }

    // ------------------------------------------------------------------
    // Reader
    // ------------------------------------------------------------------

    /**
     * Parses a compact-JSON array of INPUT fixtures, dispatching per entry on {@code kind}.
     *
     * <p>Returns {@link Event}, the common supertype, because the two kinds are not related more
     * closely than that — a real {@code AvroEvent} is not a {@code WAEvent}, and this harness's
     * mocks must not pretend otherwise. A fixture array may mix kinds; nothing needs it, and
     * forbidding it would cost a check for no benefit.</p>
     */
    public static List<Event> readInputEvents(String json) throws Exception {
        JsonNode root = MAPPER.readTree(json);
        if (root == null || !root.isArray()) {
            throw new IllegalArgumentException("input JSON fixture must be a top-level array; got: " + root);
        }
        List<Event> events = new ArrayList<>();
        int i = 0;
        for (JsonNode node : root) {
            try {
                events.add(KIND_AVRO.equals(kindOf(node))
                        ? AvroEventJsonFactory.readEvent(node)
                        : readEvent(node));
            } catch (RuntimeException e) {
                throw new IllegalArgumentException("Invalid input fixture at array index " + i
                        + ": " + e.getMessage(), e);
            }
            i++;
        }
        return events;
    }

    /** The declared {@code kind}, defaulting to {@code waevent}. */
    static String kindOf(JsonNode node) {
        return node.hasNonNull("kind") ? node.get("kind").asText() : KIND_WAEVENT;
    }

    /** Parses a compact-JSON array of WAEvent fixtures into mock {@link WAEvent}s. */
    public static List<WAEvent> readEvents(String json) throws Exception {
        JsonNode root = MAPPER.readTree(json);
        return readEvents(root);
    }

    public static List<WAEvent> readEvents(JsonNode root) {
        if (root == null || !root.isArray()) {
            throw new IllegalArgumentException("WAEvent JSON fixture must be a top-level array; got: " + root);
        }
        List<WAEvent> events = new ArrayList<>();
        int i = 0;
        for (JsonNode node : root) {
            try {
                events.add(readEvent(node));
            } catch (RuntimeException e) {
                throw new IllegalArgumentException("Invalid WAEvent fixture at array index " + i + ": " + e.getMessage(), e);
            }
            i++;
        }
        return events;
    }

    public static WAEvent readEvent(JsonNode node) {
        String kind = kindOf(node);
        if (!KIND_WAEVENT.equals(kind)) {
            // Reached when a WAEvent was required specifically -- an expected fixture, or a target
            // case's input. readInputEvents dispatches "avro" before ever calling this.
            throw new IllegalArgumentException("Unsupported event kind '" + kind + "' here (only '"
                    + KIND_WAEVENT + "' is a WAEvent; '" + KIND_AVRO + "' is input-only)");
        }

        JsonNode metadataNode = node.get("metadata");
        if (metadataNode == null || !metadataNode.isObject()) {
            throw new IllegalArgumentException("WAEvent fixture requires an object 'metadata' field");
        }

        WAEvent event = new WAEvent();
        event.typeUUID = UUID.genCurTimeUUID();
        event.metadata = toMap(metadataNode);

        JsonNode userdataNode = node.get("userdata");
        event.userdata = userdataNode != null && userdataNode.isObject() ? toMap(userdataNode) : new HashMap<>();

        // Optional: the source commit time in epoch millis, as a CDC reader stamps on the event
        // itself (SimpleEvent.originTimeStamp). Absent -> 0 -> "no source time".
        JsonNode originNode = node.get("originTimeStamp");
        if (originNode != null && !originNode.isNull()) {
            if (!originNode.canConvertToLong()) {
                throw new IllegalArgumentException("'originTimeStamp' must be epoch millis, got " + originNode);
            }
            event.originTimeStamp = originNode.asLong();
        }

        // Optional: which SOURCE published the event. A real reader stamps its own component
        // UUID on every event (SourceEvent.sourceUUID); a fixture names one with a string, and
        // the same string yields the same UUID, so two readers on one stream can be told apart
        // (§183: transactions are keyed by source AND TxnID). Absent -> null, as one source.
        JsonNode sourceNode = node.get("source");
        if (sourceNode != null && !sourceNode.isNull()) {
            if (!sourceNode.isTextual() || sourceNode.asText().isEmpty()) {
                throw new IllegalArgumentException("'source' must be a non-empty string, got " + sourceNode);
            }
            event.sourceUUID = sourceUuidFor(sourceNode.asText());
        }

        JsonNode dataNode = node.get("data");
        if (dataNode != null && !dataNode.isNull()) {
            applyImage(dataNode, event, true);
        }

        JsonNode beforeNode = node.get("before");
        if (beforeNode != null && !beforeNode.isNull()) {
            applyImage(beforeNode, event, false);
        }

        return event;
    }

    private static void applyImage(JsonNode imageNode, WAEvent event, boolean isData) {
        JsonNode valuesNode = imageNode.get("values");
        JsonNode presentNode = imageNode.get("present");
        if (valuesNode == null || presentNode == null || !valuesNode.isArray() || !presentNode.isArray()) {
            throw new IllegalArgumentException("image must have array 'values' and 'present' fields");
        }
        int n = valuesNode.size();
        if (presentNode.size() != n) {
            throw new IllegalArgumentException("'values' (" + n + ") and 'present' (" + presentNode.size() + ") must be equal length");
        }

        // Pre-size the image and bitmap directly (public fields). Present slots go through
        // setData/setBefore; an absent slot's value, if any, is written raw with its bit clear.
        if (isData) {
            event.data = new Object[n];
            event.dataPresenceBitMap = new byte[(n + 7) / 8];
        } else {
            event.before = new Object[n];
            event.beforePresenceBitMap = new byte[(n + 7) / 8];
        }

        for (int i = 0; i < n; i++) {
            Object value = toJavaValue(valuesNode.get(i));
            if (presentNode.get(i).asBoolean()) {
                if (isData) {
                    event.setData(i, value);
                } else {
                    event.setBefore(i, value);
                }
            } else if (value != null) {
                // Raw array write, bit left clear: an absent slot that still holds a value.
                (isData ? event.data : event.before)[i] = value;
            }
        }
    }

    static Object toJavaValue(JsonNode n) {
        if (n == null || n.isNull()) {
            return null;
        }
        if (n.isTextual()) {
            return n.asText();
        }
        if (n.isObject() && n.has(HEX_KEY)) {
            return decodeHex(n.get(HEX_KEY));
        }
        if (n.isBoolean()) {
            return n.asBoolean();
        }
        if (n.isInt()) {
            return n.asInt();
        }
        if (n.isLong()) {
            return n.asLong();
        }
        if (n.isBigInteger()) {
            return n.bigIntegerValue();
        }
        if (n.isFloatingPointNumber()) {
            return n.asDouble();
        }
        return n.asText();
    }

    /**
     * Decodes the {@code {"$hex": "00ff"}} form into a {@code byte[]}.
     *
     * <p>JSON has no byte type, so without this every value a fixture could feed was a string, a
     * number, a boolean or null — binary was inexpressible rather than merely untested. A bare
     * string cannot serve: strings are legitimate values, and guessing "this one looks like hex"
     * would silently turn a CHAR column holding {@code "cafe01"} into bytes. An object with one
     * reserved key cannot be mistaken for data.</p>
     *
     * <p>Decoding is strict — an odd length or a non-hex digit is refused rather than truncated,
     * because a short byte array surfaces much later as a row that does not match, naming
     * nothing.</p>
     */
    private static byte[] decodeHex(JsonNode node) {
        if (node == null || !node.isTextual()) {
            throw new IllegalArgumentException(HEX_KEY + " must be a string of hex digits, got: " + node);
        }
        String text = node.asText();
        if ((text.length() & 1) != 0) {
            throw new IllegalArgumentException(
                    HEX_KEY + " needs an even number of digits, got " + text.length() + ": " + text);
        }
        byte[] out = new byte[text.length() / 2];
        for (int i = 0; i < out.length; i++) {
            int hi = Character.digit(text.charAt(i * 2), 16);
            int lo = Character.digit(text.charAt(i * 2 + 1), 16);
            if (hi < 0 || lo < 0) {
                throw new IllegalArgumentException(
                        HEX_KEY + " has a non-hex digit at offset " + (i * 2) + ": " + text);
            }
            out[i] = (byte) ((hi << 4) | lo);
        }
        return out;
    }

    private static HashMap<String, Object> toMap(JsonNode node) {
        HashMap<String, Object> map = new HashMap<>();
        Iterator<Map.Entry<String, JsonNode>> it = node.fields();
        while (it.hasNext()) {
            Map.Entry<String, JsonNode> e = it.next();
            map.put(e.getKey(), toJavaValue(e.getValue()));
        }
        return map;
    }

    // ------------------------------------------------------------------
    // Writer
    // ------------------------------------------------------------------

    /** Serializes emitted {@link WAEvent}s back to the compact-JSON array shape. */
    public static String writeEvents(List<WAEvent> events) throws Exception {
        return MAPPER.writeValueAsString(writeEventsTree(events));
    }

    public static ArrayNode writeEventsTree(List<WAEvent> events) {
        ArrayNode array = MAPPER.createArrayNode();
        for (WAEvent event : events) {
            array.add(writeEvent(event));
        }
        return array;
    }

    public static ObjectNode writeEvent(WAEvent event) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("kind", KIND_WAEVENT);
        node.set("metadata", toJsonNode(event.metadata));
        if (event.before != null) {
            node.set("before", writeImage(event.before, event::isBeforePresent));
        }
        if (event.data != null) {
            node.set("data", writeImage(event.data, event::isDataPresent));
        }
        node.set("userdata", toJsonNode(event.userdata));
        return node;
    }

    private static ObjectNode writeImage(Object[] values, IntPredicate presence) {
        ObjectNode image = MAPPER.createObjectNode();
        ArrayNode valuesArray = MAPPER.createArrayNode();
        ArrayNode presentArray = MAPPER.createArrayNode();
        for (int i = 0; i < values.length; i++) {
            valuesArray.addPOJO(normalizeValue(values[i]));
            // Presence is derived from the bitmap read helper, never from value != null.
            presentArray.add(presence.test(i));
        }
        image.set("values", valuesArray);
        image.set("present", presentArray);
        return image;
    }

    private static ObjectNode toJsonNode(Map<String, Object> map) {
        ObjectNode node = MAPPER.createObjectNode();
        if (map != null) {
            for (Map.Entry<String, Object> e : map.entrySet()) {
                node.putPOJO(e.getKey(), normalizeValue(e.getValue()));
            }
        }
        return node;
    }

    /**
     * {@code Jackson} refuses to auto-serialize ANY type in the {@code org.joda.time.}
     * package (a package-name-prefix guard baked into its core databind: {@code
     * BeanUtil.checkUnsupportedType} rejects anything {@code BeanUtil.isJodaTimeClass}
     * matches, which is literally {@code name.startsWith("org.joda.time.")} --
     * independent of whether the class is the real Joda-Time library or this harness's
     * mock {@link DateTime}) -- it demands the
     * {@code jackson-datatype-joda} module instead. Rather than take that dependency
     * for one value type, an operator that
     * casts to a datetime column (e.g. a mapping OP with datatype casting enabled)
     * hands back a mock {@code DateTime}, and the compact WAEvent-JSON fixture format
     * only needs a plain, comparable scalar -- so this collapses it to its epoch-millis
     * {@code long} before Jackson ever sees it.
     *
     * <p>A {@code byte[]} gets the second branch, for an unrelated reason: Jackson serializes
     * one as BASE64, which is a lossy-looking form the {@code $hex} reader cannot take back.
     * It is rendered as {@code {"$hex": "..."}} so a fixture round trip returns what it stated.
     * See {@link #decodeHex}.</p>
     *
     * <p><b>Every other value passes through unchanged</b> -- deliberately: these are two
     * {@code instanceof} special cases, NOT a blanket catch, so a genuinely unserializable value
     * of any other type still fails loudly rather than being silently coerced. If a future
     * harness mock ever adds a second {@code org.joda.time.*} type, it needs its own branch here
     * (the Jackson guard above is package-wide, so it will reject that type too).</p>
     */
    private static Object normalizeValue(Object value) {
        if (value instanceof DateTime dt) {
            return dt.getMillis();
        }
        if (value instanceof byte[] bytes) {
            // Symmetric with the {"$hex": ...} reader. Without this Jackson renders a byte[] as
            // BASE64, so a fixture round trip would not return what it stated -- and a base64
            // rendering of bytes is exactly what one earlier measurement mistook for stored data.
            ObjectNode hex = MAPPER.createObjectNode();
            StringBuilder digits = new StringBuilder(bytes.length * 2);
            for (byte b : bytes) {
                digits.append(Character.forDigit((b >> 4) & 0xf, 16));
                digits.append(Character.forDigit(b & 0xf, 16));
            }
            hex.put(HEX_KEY, digits.toString());
            return hex;
        }
        return value;
    }

    /** A stable UUID for a fixture's source name: equal names, equal UUIDs. */
    static UUID sourceUuidFor(final String name) {
        long h1 = 1125899906842597L, h2 = 0x9E3779B97F4A7C15L;
        for (int i = 0; i < name.length(); i++) {
            h1 = 31 * h1 + name.charAt(i);
            h2 = 37 * h2 ^ name.charAt(i);
        }
        return new UUID(h1, h2);
    }
}
