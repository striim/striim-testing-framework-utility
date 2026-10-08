package com.example.common;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import com.webaction.runtime.meta.MetaInfo;

/**
 * Type-SHAPE comparison, and a safe get-or-create built on it.
 *
 * <p><b>The problem.</b> A Striim type is registered under a derived name, and two apps can derive
 * the SAME name for DIFFERENT shapes — the derivation is lossy (some schemes keep only a prefix per
 * column), and an upstream schema change re-derives the same name for a changed table. An event's
 * {@code data[i]} is positional against field {@code i}, so what a caller does about that matters
 * enormously and all three naive answers are wrong somewhere:
 *
 * <ul>
 *   <li><b>Adopt it</b> — writes this app's values under the other type's column names. Silent data
 *       corruption.</li>
 *   <li><b>Replace it</b> — clobbers a live type another flow may depend on.</li>
 *   <li><b>Drop and recreate it</b> — the same, plus severing dependents.</li>
 * </ul>
 *
 * <p><b>The answer this class encodes:</b> compare the shapes first, and only then let the CALLER
 * say what a genuine mismatch means. The seam can see the two schemas; it cannot see who owns the
 * name, which is the only thing that decides between refusing and overwriting. So
 * {@link OnMismatch} is a required argument with no default — a default would silently reclassify a
 * customer path.
 *
 * <p><b>Why a static composer rather than a method on {@link TypeResolver}.</b> Every operation
 * here is built from {@code TypeResolver}'s existing {@code getTypeByName} and {@code createType};
 * it adds no MDR coupling of its own, so it needs no seam of its own. Adding a method to that
 * interface would cost real money for nothing: the integration harness resolves {@code TypeResolver}
 * with a {@link java.lang.reflect.Proxy} whose fall-through throws, so every adopting module would
 * fail on its first event until the harness learned the new method — a shared-harness edit, and
 * therefore a second full rebuild-and-test gate across every consumer. A Mockito mock would return
 * {@code null} for it besides. As a static, this class costs zero interface delta, zero harness
 * edits and zero mock edits. The same reasoning, and the same measurement, produced
 * {@link TypeNames}.
 */
public final class TypeSchemas {

    /**
     * Field names a Striim type cannot carry, matched exactly. The platform generates a
     * Java bean per type — {@code WALoader.addTypeClass}, javassist, {@code extends SimpleEvent}
     * — with one public field per type field, and its generated method bodies assign to fields by
     * bare name: {@code setPayload(Object[] payload)} contains {@code payload = (T) payload[i]},
     * so a field named {@code payload} is assigned from itself and javassist rejects the class; the
     * app then terminates on its first event with no config-time diagnostic. Each name here is a
     * bare identifier in a generated body or a field the constructor assigns on 5.4
     * ({@code WALoader}, {@code SimpleEvent}):
     * {@code payload}, {@code map} ({@code setFromContextMap}), {@code kryo}/{@code output}/
     * {@code input} ({@code write}/{@code read}), {@code mapper} (a generated static field),
     * {@code fieldIsSet} (constructor), {@code key} ({@code setFromContextMap} assigns it),
     * {@code timeStamp} ({@code convertToDeleteEvent} assigns it).
     *
     * <p>Java identifiers are case-sensitive, so {@code PAYLOAD} and {@code Timestamp} are fine
     * here; the second hazard, an accessor clash, is {@link #RESERVED_ACCESSORS}.
     */
    public static final Set<String> RESERVED_FIELD_NAMES = Collections.unmodifiableSet(
            new LinkedHashSet<>(List.of("payload", "map", "kryo", "output", "input", "mapper",
                    "fieldIsSet", "key", "timeStamp")));

    /**
     * Accessor names {@code SimpleEvent} already declares. The bean generator adds
     * {@code get<Name>()}/{@code set<Name>(T)} for every field with the first letter upper-cased,
     * so a field {@code Payload} or {@code payload} produces {@code getPayload()}, which the base
     * declares with a different return type — a javassist failure — and a field {@code key} of
     * type String produces {@code getKey()}, which silently overrides the platform's partition
     * key.
     */
    public static final Set<String> RESERVED_ACCESSORS = Collections.unmodifiableSet(
            new LinkedHashSet<>(List.of("getTimeStamp", "setTimeStamp", "getPayload", "setPayload",
                    "getKey", "setKey", "getIDString", "setIDString", "getLeeEntry", "setLeeEntry",
                    "getMeteringInfo", "setMeteringInfo", "getAIData", "setAIData",
                    "getPartitionKey", "getPartitionId", "getJSONObject", "get_wa_SimpleEvent_ID",
                    "setSourceEvents")));

    /**
     * The first of {@code fieldNames} the generated bean cannot carry, or {@code null}: a name in
     * {@link #RESERVED_FIELD_NAMES}, or one whose generated accessor is in
     * {@link #RESERVED_ACCESSORS}. Pure, so every type-minting module can ask before it names a
     * field: the remedy is a Java-safe field name with the source column carried as its
     * {@code fieldAlias}, which is what the alias mechanism exists for.
     */
    public static String reservedFieldName(Collection<String> fieldNames) {
        if (fieldNames == null) {
            return null;
        }
        for (String name : fieldNames) {
            if (name == null || name.isEmpty()) {
                continue;
            }
            if (RESERVED_FIELD_NAMES.contains(name)) {
                return name;
            }
            String cap = Character.toUpperCase(name.charAt(0)) + name.substring(1);
            if (RESERVED_ACCESSORS.contains("get" + cap) || RESERVED_ACCESSORS.contains("set" + cap)) {
                return name;
            }
        }
        return null;
    }

    /**
     * Throws {@link IllegalArgumentException} naming the first reserved field, so the failure
     * is at type creation with the column named, not at the first event with a javassist
     * message about {@code incompatible type for =}.
     */
    public static void requireCreatableFieldNames(String typeName, Collection<String> fieldNames) {
        String reserved = reservedFieldName(fieldNames);
        if (reserved != null) {
            throw new IllegalArgumentException("type '" + typeName + "' cannot have a field named '"
                    + reserved + "': the platform's generated event bean already declares that member"
                    + " (SimpleEvent field or generated method parameter), and the app would terminate"
                    + " on its first event. Give the field a Java-safe name and carry '" + reserved
                    + "' as its fieldAlias.");
        }
    }

    private TypeSchemas() {
    }

    /** What {@link #getOrCreate} does when a type of that name exists with a DIFFERENT shape. */
    public enum OnMismatch {
        /**
         * Throw {@link TypeSchemaMismatchException}. Correct whenever the caller cannot know it
         * owns the name — above all when the name is a lossy derivation, where another app's type
         * can land on it legitimately.
         */
        REFUSE,
        /**
         * Overwrite the existing type, logging both schemas first. The caller is asserting that it
         * owns this name and that only its own shape matters. This is the silent default elsewhere;
         * choosing it here keeps the behaviour and adds the missing log line.
         */
        REPLACE
    }

    /**
     * The FIRST facet on which the intended schema and an existing type disagree, or {@code null}
     * when the existing type may safely be adopted.
     *
     * <p>Five facets, in the order a mismatch is most likely and most damaging:
     * field <b>count</b>; field <b>names in order</b> (positional, so order is not cosmetic);
     * per-field <b>Java type</b>; <b>key fields</b> (order-independent — a set, not a list); and
     * <b>significant aliases</b> (see {@link #significantAliases}).
     *
     * <p><b>Unreadable is a mismatch, not a match.</b> If the existing type's field map cannot be
     * read this returns a mismatch, so the caller refuses or replaces rather than adopting a type
     * it could not inspect. Failing closed is the only safe direction here.
     */
    public static String firstMismatch(MetaInfo.Type existing, Map<String, String> fields,
            Map<String, String> fieldAliases, Map<String, Boolean> fieldKeys) {

        if (fields == null) {
            return "the intended schema has no fields";
        }
        if (existing == null) {
            return "there is no existing type to compare against";
        }
        // Direct field access, NOT reflection. MetaInfo.Type declares these public, and Platform is
        // on the compile classpath -- so a platform rename becomes a COMPILE ERROR here rather than
        // a runtime read failure that would present as a phantom collision on every type.
        final Map<String, String> actualFields = existing.fields;
        final Map<String, String> actualAliases = existing.fieldAlias;
        final List<String> actualKeyFields = existing.keyFields;

        if (actualFields == null) {
            return "the existing type's field map could not be read";
        }

        // 1 -- field count
        final List<String> expectedNames = new ArrayList<>(fields.keySet());
        final List<String> actualNames = new ArrayList<>(actualFields.keySet());
        if (expectedNames.size() != actualNames.size()) {
            return "field COUNT differs (this app needs " + expectedNames.size()
                    + ", the existing type has " + actualNames.size() + ")";
        }

        // 2 -- field names, IN ORDER (data[i] is positional against field i), compared
        // CASE-INSENSITIVELY: the platform lowercases every field name on the way in. Measured
        // against a live MDR -- an operator that registered ORDER_ID reads back `order_id`. A
        // case-sensitive compare here reports a phantom mismatch on every field of every type,
        // forever. ORDER is still significant; only case is not.
        if (!lower(expectedNames).equals(lower(actualNames))) {
            return "field NAMES (or their ORDER) differ";
        }

        // 3 -- Java type per field, compared through sameType(): the platform EXPANDS a short
        // type name to its fully-qualified form. Measured against a live MDR -- an operator that
        // registered "Integer" reads back "java.lang.Integer", "String" -> "java.lang.String",
        // "DateTime" -> "org.joda.time.DateTime". Comparing the raw strings reports a phantom
        // mismatch on every field of every type.
        final Map<String, String> actualByLowerName = lowerKeyed(actualFields);
        for (final String fieldName : expectedNames) {
            final String expectedType = fields.get(fieldName);
            final String actualType = actualByLowerName.get(fieldName.toLowerCase(java.util.Locale.ROOT));
            if (!sameType(expectedType, actualType)) {
                return "field '" + fieldName + "' has TYPE " + actualType
                        + " but this app needs " + expectedType;
            }
        }

        // 4 -- key fields, order-independent: keyFields is a set of names, not a positional list
        final Set<String> expectedKeys = keyFieldNames(fieldKeys);
        final Set<String> actualKeys = actualKeyFields == null
                ? new LinkedHashSet<>() : new LinkedHashSet<>(actualKeyFields);
        if (!lowerSet(expectedKeys).equals(lowerSet(actualKeys))) {
            return "KEY FIELDS differ (this app needs " + expectedKeys
                    + ", the existing type has " + actualKeys + ")";
        }

        // 5 -- significant aliases
        final Map<String, String> expectedAliases = significantAliases(fieldAliases);
        final Map<String, String> actual = significantAliases(actualAliases);
        if (!expectedAliases.equals(actual)) {
            return "field ALIASES differ (this app needs " + expectedAliases
                    + ", the existing type has " + actual + ")";
        }

        return null;
    }

    /** {@code EXPECTED … || ACTUAL …} for one log line, so a collision is diagnosable at 2am. */
    public static String describeCollision(MetaInfo.Type existing, Map<String, String> fields,
            Map<String, String> fieldAliases, Map<String, Boolean> fieldKeys) {
        return "EXPECTED " + describeSchema(fields, keyFieldNames(fieldKeys),
                        significantAliases(fieldAliases))
                + " || ACTUAL " + (existing == null ? "(no existing type)"
                        : describeSchema(existing.fields,
                                existing.keyFields == null ? new LinkedHashSet<>()
                                        : new LinkedHashSet<>(existing.keyFields),
                                significantAliases(existing.fieldAlias)));
    }

    /**
     * Absent → create. Exists with a matching shape → adopt. Exists with a different shape → per
     * {@code onMismatch}.
     *
     * @param typeName fully-qualified {@code namespace.name}, as {@link TypeNames#forTable} builds
     *     it. Split with {@link TypeNames#namespaceOf}/{@link TypeNames#simpleNameOf}, which is
     *     what makes a name carrying more than one dot resolve correctly.
     * @throws TypeSchemaMismatchException under {@link OnMismatch#REFUSE}
     */
    public static MetaInfo.Type getOrCreate(TypeResolver types, String typeName,
            Map<String, String> fields, Map<String, String> fieldAliases,
            Map<String, Boolean> fieldKeys, OnMismatch onMismatch, Logger logger) throws Exception {

        if (onMismatch == null) {
            throw new IllegalArgumentException(
                    "onMismatch is required: only the caller knows whether it owns this type name, "
                            + "and that is what decides between refusing and overwriting");
        }

        // Before the lookup: a type that cannot be created is refused with the column named,
        // whatever the repository holds.
        requireCreatableFieldNames(typeName, fields == null ? null : fields.keySet());

        final MetaInfo.Type existing =
                types.getTypeByName(TypeNames.namespaceOf(typeName), TypeNames.simpleNameOf(typeName));

        if (existing != null) {
            final String mismatch = firstMismatch(existing, fields, fieldAliases, fieldKeys);
            if (mismatch == null) {
                log(logger, () -> "TypeSchemas: adopting existing type '" + typeName
                        + "' — its shape matches");
                // Adopted from the MDR, so its class may exist only in a JVM that is gone (a
                // restart after a crash); regenerate it here or the consumers see no fields.
                types.ensureClass(existing);
                return existing;
            }
            final String detail = describeCollision(existing, fields, fieldAliases, fieldKeys);
            if (onMismatch == OnMismatch.REFUSE) {
                logError(logger, () -> "TypeSchemas: TYPE NAME COLLISION on '" + typeName + "' — "
                        + mismatch + ". REFUSING. " + detail);
                throw new TypeSchemaMismatchException(typeName, mismatch, detail);
            }
            logWarn(logger, () -> "TypeSchemas: type '" + typeName + "' already exists with a "
                    + "different shape — " + mismatch + ". REPLACING it, as this caller's policy "
                    + "asserts it owns the name. " + detail);
        }

        return types.createType(typeName, fields, fieldAliases, fieldKeys);
    }

    // ---- helpers, package-private so their own tests can reach them ----

    /**
     * Aliases that actually mean something: an alias equal to its own field name, or null/blank, is
     * indistinguishable from having no alias at all, so it must not make two schemas differ. This
     * is what lets a caller passing {@code Map.of()} match a type registered with self-identical
     * aliases.
     */
    static Map<String, String> significantAliases(Map<String, String> aliases) {
        final Map<String, String> out = new LinkedHashMap<>();
        if (aliases == null) {
            return out;
        }
        aliases.forEach((field, alias) -> {
            // equalsIgnoreCase, not equals: the platform lowercases the field name and keeps the
            // ORIGINAL casing in the alias, so a type registered with no aliases at all reads back
            // with fieldAlias=ORDER_ID against field order_id. Treating that as a genuine rename
            // makes every such type collide with the caller that created it. Measured on a live MDR.
            if (alias != null && !alias.trim().isEmpty() && !alias.equalsIgnoreCase(field)) {
                out.put(field.toLowerCase(java.util.Locale.ROOT),
                        alias.toLowerCase(java.util.Locale.ROOT));
            }
        });
        return out;
    }

    /**
     * Whether two Java-type strings name the same type, allowing for the platform expanding a
     * short name to its fully-qualified form ({@code Integer} -> {@code java.lang.Integer}).
     *
     * <p>Suffix-on-a-dot-boundary, not simple-name equality: {@code java.sql.Date} and
     * {@code java.util.Date} are both fully qualified and neither is the other's short form, so
     * they still differ. A caller supplying the bare {@code Date} cannot distinguish them — but a
     * caller supplying the bare name has not asked to.
     */
    static boolean sameType(String expected, String actual) {
        if (expected == null || actual == null) {
            return expected == null && actual == null;
        }
        if (expected.equals(actual)) {
            return true;
        }
        return isShortFormOf(expected, actual) || isShortFormOf(actual, expected);
    }

    private static boolean isShortFormOf(String maybeShort, String maybeQualified) {
        return maybeShort.indexOf('.') < 0 && maybeQualified.endsWith("." + maybeShort);
    }

    private static List<String> lower(List<String> names) {
        final List<String> out = new ArrayList<>(names.size());
        for (String n : names) {
            out.add(n == null ? null : n.toLowerCase(java.util.Locale.ROOT));
        }
        return out;
    }

    private static Set<String> lowerSet(Set<String> names) {
        final Set<String> out = new LinkedHashSet<>();
        for (String n : names) {
            out.add(n == null ? null : n.toLowerCase(java.util.Locale.ROOT));
        }
        return out;
    }

    private static Map<String, String> lowerKeyed(Map<String, String> m) {
        final Map<String, String> out = new LinkedHashMap<>();
        m.forEach((k, v) -> out.put(k == null ? null : k.toLowerCase(java.util.Locale.ROOT), v));
        return out;
    }

    /** The names flagged as key fields; an absent or all-false map yields an empty set. */
    static Set<String> keyFieldNames(Map<String, Boolean> fieldKeys) {
        final Set<String> out = new LinkedHashSet<>();
        if (fieldKeys == null) {
            return out;
        }
        fieldKeys.forEach((field, isKey) -> {
            if (Boolean.TRUE.equals(isKey)) {
                out.add(field);
            }
        });
        return out;
    }

    /**
     * Renders one schema as {@code fields=[NAME:type, …] keyFields=[…] fieldAlias={…}}.
     *
     * <p>The field list is built by hand rather than left to {@code Map.toString()}, and that is
     * deliberate: a map renders as {@code {NAME=type}}, so a reader scanning a wrapped 2am log
     * line cannot tell a field separator from a value that happens to contain {@code =}. The
     * {@code NAME:type} form has no such ambiguity.
     */
    private static String describeSchema(Map<String, String> fields, Set<String> keys,
            Map<String, String> aliases) {
        final StringBuilder sb = new StringBuilder("fields=");
        if (fields == null) {
            sb.append("<unreadable>");
        } else {
            sb.append('[');
            boolean first = true;
            for (final Map.Entry<String, String> entry : fields.entrySet()) {
                if (!first) {
                    sb.append(", ");
                }
                sb.append(entry.getKey()).append(':').append(entry.getValue());
                first = false;
            }
            sb.append(']');
        }
        return sb.append(" keyFields=").append(keys)
                .append(" fieldAlias=").append(aliases).toString();
    }

    private static void log(Logger l, java.util.function.Supplier<String> m) {
        if (l != null) l.log(m);
    }

    private static void logWarn(Logger l, java.util.function.Supplier<String> m) {
        if (l != null) l.logWarn(m);
    }

    private static void logError(Logger l, java.util.function.Supplier<String> m) {
        if (l != null) l.logError(m);
    }
}
