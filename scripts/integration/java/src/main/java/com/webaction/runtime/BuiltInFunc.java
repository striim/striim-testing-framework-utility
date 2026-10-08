package com.webaction.runtime;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;

/**
 * Minimal mock of Striim's {@code com.webaction.runtime.BuiltInFunc} -- a concrete
 * platform utility class (distinct from the {@code com.example.common.BuiltInFuncs}
 * seam interface, despite the similar name), referenced directly by operators that cast
 * values to typed columns (e.g. a mapping OP's {@code DataTypeConverter}). Only the
 * {@code TO_*} statics {@code DataTypeConverter} calls are reproduced, with ordinary Java
 * conversion semantics -- not a byte-for-byte reimplementation of Striim's real conversion
 * engine -- sufficient to prove a casting operator's behavior end-to-end through the
 * harness without linking real Striim jars. {@code TO_DATE} returns the harness's own
 * mock {@link org.joda.time.DateTime} (real Joda-Time is a Striim {@code system}-scope,
 * compile-only dependency never shaded into an OP jar) -- both overloads exist because
 * {@code DataTypeConverter.convertToDateTime} calls {@code TO_DATE} from branches whose
 * argument has static type {@code long} (already-extracted epoch millis) or {@code Object}
 * (a {@code String} value, statically typed as the enclosing method's {@code Object}
 * parameter) -- exactly the two descriptors the real {@code BuiltInFunc} declares.
 *
 * <p>
 * <b>{@code IS_PRESENT}/{@code getIndexOfColumn}</b> (added for {@code UdfCore})
 * exist because a UDF may call these as STATIC methods baked
 * directly into its own bytecode -- unlike an OpenProcessor's {@code BuiltInFuncs}
 * constructor parameter (a {@link java.lang.reflect.Proxy} seam {@code
 * IntegrationProcessor.newBuiltInFuncsProxy} intercepts per-instance), there is no
 * per-instance seam here to proxy: the class itself must exist on the classpath with real
 * method bodies. {@code IS_PRESENT} is written to be byte-for-byte behaviorally identical
 * to {@code newBuiltInFuncsProxy}'s existing {@code IS_PRESENT} case (same reference-
 * compare-then-delegate, including the no-null-guard edge case -- see that method's own
 * javadoc) -- but it is a genuinely NEW static descriptor on the classpath, not merely an
 * internal refactor: OPs may already call {@code
 * BuiltInFunc.IS_PRESENT} statically, and before this addition those calls threw {@code
 * NoSuchMethodError} against this mock (a loud harness gap) rather than resolving. No
 * fixture drives either OP today, so nothing currently observes the change, but the OP
 * path is not categorically "unaffected" the way {@code getIndexOfColumn} is -- confirm
 * the new semantics are still correct for a given OP's actual call sites before authoring
 * its integration fixture. {@code getIndexOfColumn} resolves a column name against {@link
 * #columnSchemas}, a small static registry seeded ONLY by {@code UdfCore.build} from a
 * {@code test.yaml}'s {@code types:} block (the same source {@code
 * newBuiltInFuncsProxy}'s {@code getFieldsArray}/{@code getAliasFieldName} cases already
 * read) -- {@code OperatorCore} never touches this registry, so no OP's behavior changes
 * on that axis; no shipped OP calls {@code getIndexOfColumn} statically today.
 */
public final class BuiltInFunc {

    private BuiltInFunc() {
    }

    /** Ordered source column names by source {@code metadata.TableName}, seeded ONLY by {@code UdfCore.build}. */
    private static volatile Map<String, List<String>> columnSchemas = Map.of();

    /**
     * Replaces the column-schema registry (never merges), so a second {@code
     * UdfCore.build} call in the same JVM (e.g. the JUnit suite driving more than one
     * fixture) cannot leak a previous test's schemas. A {@code null} map clears it.
     */
    public static void setColumnSchemas(Map<String, List<String>> schemasByTable) {
        columnSchemas = schemasByTable == null ? Map.of() : schemasByTable;
    }

    /**
     * Positional source fields by declared type UUID, seeded ONLY by {@code TargetCore.build}.
     *
     * <p><b>Why the target path needs a static registry when the OP path does not.</b> An
     * OpenProcessor is HANDED a {@code BuiltInFuncs} proxy through constructor injection, so the
     * harness can answer per-instance. A Target is constructed by the platform through a public
     * no-arg constructor and builds its own {@code BuiltInFuncResolver}, which calls these statics
     * — there is no seam to inject. That is the production path, so a target case exercises the
     * real resolver rather than a stand-in for it.</p>
     */
    private static volatile Map<com.webaction.uuid.UUID, java.lang.reflect.Field[]> sourceFields =
            Map.of();

    /** Field-name aliases by declared type UUID; the companion to {@link #sourceFields}. */
    private static volatile Map<com.webaction.uuid.UUID, Map<String, String>> sourceAliases =
            Map.of();

    /**
     * Replaces the source-type registry (never merges), so a second {@code TargetCore.build} in
     * the same JVM cannot leak a previous case's schemas. Null maps clear it.
     *
     * <p>Takes already-built fields and aliases rather than the column names it would derive them
     * from: this is a mock of a PLATFORM class, and having it reach back into the harness's own
     * {@code MockSourceFields} would make the two packages mutually dependent. The caller owns the
     * shape; this only serves it.</p>
     *
     * @param fieldsByUuid  positional placeholder fields per declared type
     * @param aliasesByUuid the {@code f<i> -> column} map those placeholders are renamed through
     */
    public static void setSourceTypes(
            Map<com.webaction.uuid.UUID, java.lang.reflect.Field[]> fieldsByUuid,
            Map<com.webaction.uuid.UUID, Map<String, String>> aliasesByUuid) {
        sourceFields = fieldsByUuid == null ? Map.of() : fieldsByUuid;
        sourceAliases = aliasesByUuid == null ? Map.of() : aliasesByUuid;
    }

    /**
     * The source type's positional fields, or an empty array when the type is not declared.
     *
     * <p>Empty rather than null, and that distinction is load-bearing: the real method returns the
     * reflected fields of a generated class, and a writer reading a type it was never given a
     * {@code types:} entry for should see "no columns", not an NPE from deep inside its own
     * alignment code where the cause is unrecoverable.</p>
     */
    public static java.lang.reflect.Field[] getFieldsArray(com.webaction.uuid.UUID sourceTypeUUID) {
        java.lang.reflect.Field[] fields = sourceFields.get(sourceTypeUUID);
        return fields == null ? new java.lang.reflect.Field[0] : fields;
    }

    /** The source type's field-name aliases, or an empty map when the type is not declared. */
    public static Map<String, String> getAliasFieldName(com.webaction.uuid.UUID sourceTypeUUID) {
        Map<String, String> aliases = sourceAliases.get(sourceTypeUUID);
        return aliases == null ? Map.of() : aliases;
    }

    /**
     * Whether the column at {@code index} is present in {@code image} (matched to {@code
     * event.data}/{@code event.before} by reference). Delegates straight to the mock
     * {@link WAEvent}'s own bitmap read helpers -- this mock's {@code IS_PRESENT} must be
     * internally consistent with the mock {@code WAEvent}'s bit layout, not with the real
     * platform class's, exactly like {@code IntegrationProcessor.newBuiltInFuncsProxy}'s
     * existing {@code IS_PRESENT} case.
     */
    public static boolean IS_PRESENT(WAEvent event, Object[] image, int index) {
        if (event == null) {
            return false;
        }
        // No `image != null` guard: `newBuiltInFuncsProxy`'s existing IS_PRESENT case has
        // none either, so `image == event.before` correctly (and harmlessly) evaluates
        // true when BOTH are null -- omitting the guard here, not adding one, is what
        // keeps this static byte-for-byte behaviorally identical to that proxy case for
        // every input, including a null image against a before-less (insert) event.
        if (image == event.before) {
            return event.isBeforePresent(index);
        }
        return event.isDataPresent(index);
    }

    /**
     * Resolves {@code column}'s 0-based positional index from the schema registered for
     * {@code event.metadata.TableName} (case-insensitive match, mirroring the real
     * method's field-name fallback path). Returns {@code -1} when the event, its table,
     * or the column itself is unknown -- never throws, matching the real method's {@code
     * -1}-on-miss contract (UDF callers commonly catch {@code Throwable -> -1} regardless).
     */
    public static int getIndexOfColumn(WAEvent event, String column) {
        if (event == null || column == null || event.metadata == null) {
            return -1;
        }
        Object table = event.metadata.get("TableName");
        if (table == null) {
            return -1;
        }
        List<String> columns = columnSchemas.get(String.valueOf(table));
        if (columns == null) {
            return -1;
        }
        for (int i = 0; i < columns.size(); i++) {
            if (columns.get(i).equalsIgnoreCase(column)) {
                return i;
            }
        }
        return -1;
    }

    public static Integer TO_INT(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.intValue();
        }
        return Integer.parseInt(value.toString().trim());
    }

    public static Long TO_LONG(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.longValue();
        }
        return Long.parseLong(value.toString().trim());
    }

    public static Float TO_FLOAT(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.floatValue();
        }
        return Float.parseFloat(value.toString().trim());
    }

    public static Double TO_DOUBLE(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.doubleValue();
        }
        return Double.parseDouble(value.toString().trim());
    }

    public static Boolean TO_BOOLEAN(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Boolean b) {
            return b;
        }
        return Boolean.parseBoolean(value.toString().trim());
    }

    public static String TO_STRING(Object value) {
        return value == null ? null : value.toString();
    }

    public static byte[] TO_BYTE_ARRAY(Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof byte[] b) {
            return b;
        }
        return value.toString().getBytes(StandardCharsets.UTF_8);
    }

    /**
     * Parses Striim's serialized WAEvent array back into events — the platform function an
     * exception-store reader uses to reconstruct the event a stored {@code relatedObjects} payload
     * came from.
     *
     * <p><b>This parses the PRODUCTION dialect, deliberately, and not the harness's fixture
     * dialect.</b> A stored payload is written by the product, so it looks like
     * {@code [{"metadata":{...},"data":["1","ACME"]}]} — {@code data} is a bare array. The
     * harness's own fixtures instead write {@code "data":{"values":[...],"present":[...]}} because a
     * fixture must be able to express column presence. Delegating to the fixture reader was tried
     * and is wrong: it would force cases to store a payload no real store ever contains, and the
     * tier would then prove only that the mock agrees with the fixture format.</p>
     *
     * <p>Every parsed column is written through {@link WAEvent#setData} so the presence bitmap is
     * set — assigning {@code data[i]} directly leaves a column that later reads as absent, which is
     * the single most common way a hand-built WAEvent goes subtly wrong.</p>
     *
     * <p>Declared {@code throws Exception} because the real one is: a core catching parse failure
     * around this call is exercising a real branch, not dead code.</p>
     */
    public static List<WAEvent> to_WAEvent(String json) throws Exception {
        com.fasterxml.jackson.databind.JsonNode root =
                new com.fasterxml.jackson.databind.ObjectMapper().readTree(json);
        if (!root.isArray()) {
            throw new IllegalArgumentException(
                    "to_WAEvent expects a JSON array of serialized WAEvents, got: " + root.getNodeType());
        }
        List<WAEvent> events = new java.util.ArrayList<WAEvent>();
        for (int i = 0; i < root.size(); i++) {
            com.fasterxml.jackson.databind.JsonNode node = root.get(i);
            WAEvent event = new WAEvent();
            com.fasterxml.jackson.databind.JsonNode metadata = node.path("metadata");
            java.util.Iterator<String> names = metadata.fieldNames();
            while (names.hasNext()) {
                String name = names.next();
                event.metadata.put(name, scalar(metadata.get(name)));
            }
            fill(node.path("data"), event, true);
            fill(node.path("before"), event, false);
            events.add(event);
        }
        return events;
    }

    /** Writes one image's columns through setData/setBefore so the presence bitmap is maintained. */
    private static void fill(com.fasterxml.jackson.databind.JsonNode image, WAEvent event, boolean isData) {
        if (!image.isArray()) {
            return;
        }
        if (isData) {
            event.data = new Object[image.size()];
            event.dataPresenceBitMap = new byte[(image.size() + 7) / 8];
        } else {
            event.before = new Object[image.size()];
            event.beforePresenceBitMap = new byte[(image.size() + 7) / 8];
        }
        for (int i = 0; i < image.size(); i++) {
            // EVERY element is PRESENT, including a JSON null. The production dialect has no
            // presence array, so a serialized image cannot express absence at all -- every column
            // it contains was written. A null therefore means "present, value null", and skipping
            // setData here would silently reconstruct the column as ABSENT: the exact
            // presence-bitmap bug the plugin rules warn about, produced by the harness rather than
            // by an op.
            com.fasterxml.jackson.databind.JsonNode column = image.get(i);
            if (isData) {
                event.setData(i, scalar(column));
            } else {
                event.setBefore(i, scalar(column));
            }
        }
    }

    /** JSON scalar to the Java value a WAEvent column holds. */
    private static Object scalar(com.fasterxml.jackson.databind.JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        if (node.isBoolean()) {
            return Boolean.valueOf(node.asBoolean());
        }
        if (node.isInt() || node.isLong()) {
            return Long.valueOf(node.asLong());
        }
        if (node.isFloatingPointNumber()) {
            return Double.valueOf(node.asDouble());
        }
        return node.asText();
    }

    public static org.joda.time.DateTime TO_DATE(long epochMillis) {
        return new org.joda.time.DateTime(epochMillis);
    }

    /** Parses an ISO-8601 instant/date string; a non-{@code String} value is rejected. */
    public static org.joda.time.DateTime TO_DATE(Object value) {
        if (value == null) {
            return null;
        }
        String text = value.toString().trim();
        try {
            return new org.joda.time.DateTime(java.time.Instant.parse(text).toEpochMilli());
        } catch (java.time.format.DateTimeParseException e) {
            return new org.joda.time.DateTime(
                    java.time.LocalDate.parse(text).atStartOfDay(java.time.ZoneOffset.UTC).toInstant().toEpochMilli());
        }
    }
}
