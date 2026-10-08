package com.striim.testing.inttest;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import org.apache.avro.Schema;
import org.apache.avro.generic.GenericData;
import org.apache.avro.generic.GenericRecord;
import com.webaction.proc.events.AvroEvent;

/**
 * Reader for the {@code "kind": "avro"} input fixture — the sibling branch
 * {@link WAEventJsonFactory} reserved for a second event kind:
 *
 * <pre>
 * {
 *   "kind":     "avro",
 *   "schema":   { ... an Avro schema, in Avro's own JSON schema syntax ... },  // required
 *   "record":   { ... the value, in PLAIN JSON ... },                          // required
 *   "metadata": { "KafkaRecordTimestamp": 1730000000000 },                     // optional
 *   "userdata": { }                                                            // optional
 * }
 * </pre>
 *
 * <p><b>{@code record} is plain JSON, not Avro's JSON encoding.</b> Avro would demand
 * {@code {"after": {"my.ns.Value": {"id": {"int": 1}}}}} for the nullable unions a Debezium
 * envelope is made of — every field wrapped in its branch name. That is unreadable and
 * unreviewable in a fixture, and a reviewer's inability to see what a case actually feeds is how a
 * case ends up asserting something other than it claims. So the value is written the way the data
 * looks, and this class resolves it against {@code schema}: a union takes its null branch for a
 * JSON null and otherwise the first branch whose type the value fits.
 *
 * <p><b>There is no writer.</b> Nothing emits an {@code AvroEvent} — an operator taking one
 * converts it to {@code WAEvent}s — so the output half of the fixture format stays
 * {@link WAEventJsonFactory}'s alone.
 */
final class AvroEventJsonFactory {

    private AvroEventJsonFactory() {
    }

    /** Builds one {@link AvroEvent} from a fixture node already known to be {@code kind: avro}. */
    static AvroEvent readEvent(JsonNode node) {
        JsonNode schemaNode = node.get("schema");
        if (schemaNode == null || !schemaNode.isObject()) {
            throw new IllegalArgumentException(
                    "an avro fixture requires an object 'schema' field (Avro schema JSON)");
        }
        JsonNode recordNode = node.get("record");
        if (recordNode == null || !recordNode.isObject()) {
            throw new IllegalArgumentException(
                    "an avro fixture requires an object 'record' field");
        }

        Schema schema;
        try {
            schema = new Schema.Parser().parse(schemaNode.toString());
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("avro fixture 'schema' is not a valid Avro schema: "
                    + e.getMessage(), e);
        }
        if (schema.getType() != Schema.Type.RECORD) {
            throw new IllegalArgumentException("avro fixture 'schema' must be a record schema; got "
                    + schema.getType());
        }

        AvroEvent event = new AvroEvent();
        event.data = (GenericRecord) toAvro(recordNode, schema, "record");
        event.metadata = toMap(node.get("metadata"));
        event.userdata = toMap(node.get("userdata"));
        return event;
    }

    /**
     * One JSON value as the Avro datum {@code schema} calls for.
     *
     * @param path the dotted position in the fixture, so a failure names WHERE rather than only
     *     what — a 40-field Debezium envelope is otherwise a guessing game
     */
    private static Object toAvro(JsonNode value, Schema schema, String path) {
        switch (schema.getType()) {
            case UNION:
                return toAvroUnion(value, schema, path);
            case NULL:
                requireNull(value, schema, path);
                return null;
            case RECORD:
                return toAvroRecord(value, schema, path);
            case ARRAY: {
                if (value == null || !value.isArray()) {
                    throw mismatch(value, schema, path);
                }
                List<Object> items = new ArrayList<>();
                int i = 0;
                for (JsonNode item : value) {
                    items.add(toAvro(item, schema.getElementType(), path + "[" + i++ + "]"));
                }
                return items;
            }
            case MAP: {
                if (value == null || !value.isObject()) {
                    throw mismatch(value, schema, path);
                }
                Map<String, Object> map = new LinkedHashMap<>();
                Iterator<Map.Entry<String, JsonNode>> it = value.fields();
                while (it.hasNext()) {
                    Map.Entry<String, JsonNode> e = it.next();
                    map.put(e.getKey(), toAvro(e.getValue(), schema.getValueType(),
                            path + "." + e.getKey()));
                }
                return map;
            }
            case ENUM: {
                if (value == null || !value.isTextual()) {
                    throw mismatch(value, schema, path);
                }
                if (!schema.hasEnumSymbol(value.asText())) {
                    throw new IllegalArgumentException("avro fixture at " + path + ": '"
                            + value.asText() + "' is not a symbol of enum " + schema.getFullName()
                            + " " + schema.getEnumSymbols());
                }
                return new GenericData.EnumSymbol(schema, value.asText());
            }
            case BYTES:
                return ByteBuffer.wrap(decodeBase64(value, schema, path));
            case FIXED: {
                byte[] bytes = decodeBase64(value, schema, path);
                if (bytes.length != schema.getFixedSize()) {
                    throw new IllegalArgumentException("avro fixture at " + path + ": fixed "
                            + schema.getFullName() + " is " + schema.getFixedSize()
                            + " bytes, got " + bytes.length);
                }
                return new GenericData.Fixed(schema, bytes);
            }
            case STRING:
                if (value == null || !value.isTextual()) {
                    throw mismatch(value, schema, path);
                }
                return value.asText();
            case BOOLEAN:
                if (value == null || !value.isBoolean()) {
                    throw mismatch(value, schema, path);
                }
                return value.asBoolean();
            case INT:
                if (value == null || !value.isIntegralNumber() || !value.canConvertToInt()) {
                    throw mismatch(value, schema, path);
                }
                return value.asInt();
            case LONG:
                if (value == null || !value.isIntegralNumber()) {
                    throw mismatch(value, schema, path);
                }
                return value.asLong();
            case FLOAT:
                if (value == null || !value.isNumber()) {
                    throw mismatch(value, schema, path);
                }
                return (float) value.asDouble();
            case DOUBLE:
                if (value == null || !value.isNumber()) {
                    throw mismatch(value, schema, path);
                }
                return value.asDouble();
            default:
                throw new IllegalArgumentException("avro fixture at " + path
                        + ": unsupported schema type " + schema.getType());
        }
    }

    private static GenericRecord toAvroRecord(JsonNode value, Schema schema, String path) {
        if (value == null || !value.isObject()) {
            throw mismatch(value, schema, path);
        }
        // Reject a field the schema does not declare rather than dropping it. A typo in a fixture
        // key otherwise reads as "the operator ignored my column", which is a real defect's
        // symptom, and the case would be asserting against a record it never actually built.
        Iterator<String> supplied = value.fieldNames();
        while (supplied.hasNext()) {
            String name = supplied.next();
            if (schema.getField(name) == null) {
                throw new IllegalArgumentException("avro fixture at " + path + ": '" + name
                        + "' is not a field of record " + schema.getFullName() + " "
                        + schema.getFields().stream().map(Schema.Field::name).toList());
            }
        }
        GenericRecord record = new GenericData.Record(schema);
        for (Schema.Field field : schema.getFields()) {
            JsonNode fieldValue = value.get(field.name());
            if (fieldValue == null) {
                // Absent, as distinct from an explicit null. The slot stays as GenericData.Record
                // left it, which is NULL -- Avro schema defaults are not applied here, and saying
                // otherwise would be wrong. A fixture that means a value should write one.
                continue;
            }
            record.put(field.pos(), toAvro(fieldValue, field.schema(), path + "." + field.name()));
        }
        return record;
    }

    /**
     * The union branch this value belongs to.
     *
     * <p>Null takes the null branch. Anything else takes the FIRST branch it fits. An ambiguous
     * union (two branches a JSON number fits, say) resolves to the earlier one — declared
     * behaviour, not an accident, and the reason a fixture wanting the other branch should say so
     * in its schema.</p>
     *
     * <p>⚠ <b>A union with exactly one non-null branch does NOT get the try-each treatment</b>,
     * and that is the whole difference between a usable error and a useless one. {@code
     * ["null", "Value"]} is what Debezium emits for every field of an envelope, so the commonest
     * fixture mistake by far is a typo INSIDE that branch — and swallowing the branch's own
     * exception to report "fits no branch of union &lt;the entire schema&gt;" hides
     * {@code 'idd' is not a field of record Value [id]} behind a wall of JSON. With one candidate
     * there is nothing to disambiguate, so its failure IS the failure.</p>
     */
    private static Object toAvroUnion(JsonNode value, Schema schema, String path) {
        List<Schema> branches = schema.getTypes();
        if (value == null || value.isNull()) {
            for (Schema branch : branches) {
                if (branch.getType() == Schema.Type.NULL) {
                    return null;
                }
            }
            throw new IllegalArgumentException("avro fixture at " + path
                    + ": null, but the union has no null branch " + branches);
        }

        List<Schema> candidates = new ArrayList<>();
        for (Schema branch : branches) {
            if (branch.getType() != Schema.Type.NULL) {
                candidates.add(branch);
            }
        }
        if (candidates.size() == 1) {
            return toAvro(value, candidates.get(0), path);
        }
        for (Schema branch : candidates) {
            try {
                return toAvro(value, branch, path);
            } catch (IllegalArgumentException ignored) {
                // Not this branch; try the next. With more than one candidate the throw below
                // reports the whole union, which is more useful than whichever branch happened
                // to be attempted last.
            }
        }
        throw new IllegalArgumentException("avro fixture at " + path + ": " + describe(value)
                + " fits no branch of union " + branches);
    }

    private static byte[] decodeBase64(JsonNode value, Schema schema, String path) {
        if (value == null || !value.isTextual()) {
            throw mismatch(value, schema, path);
        }
        try {
            return Base64.getDecoder().decode(value.asText());
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("avro fixture at " + path + ": " + schema.getType()
                    + " must be base64 text; got " + describe(value), e);
        }
    }

    private static void requireNull(JsonNode value, Schema schema, String path) {
        if (value != null && !value.isNull()) {
            throw mismatch(value, schema, path);
        }
    }

    private static IllegalArgumentException mismatch(JsonNode value, Schema schema, String path) {
        return new IllegalArgumentException("avro fixture at " + path + ": expected "
                + schema.getType() + ", got " + describe(value));
    }

    private static String describe(JsonNode value) {
        return value == null ? "an absent field" : value.getNodeType() + " " + value;
    }

    private static HashMap<String, Object> toMap(JsonNode node) {
        HashMap<String, Object> map = new HashMap<>();
        if (node != null && node.isObject()) {
            Iterator<Map.Entry<String, JsonNode>> it = node.fields();
            while (it.hasNext()) {
                Map.Entry<String, JsonNode> e = it.next();
                map.put(e.getKey(), WAEventJsonFactory.toJavaValue(e.getValue()));
            }
        }
        return map;
    }
}
