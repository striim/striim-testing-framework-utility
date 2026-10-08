package com.webaction.runtime.meta;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.webaction.uuid.UUID;

/**
 * Minimal mock of Striim's {@code com.webaction.runtime.meta.MetaInfo}. Only the nested
 * {@code Type} is reproduced, and only the surface an operator under test actually touches.
 *
 * <p>
 * That surface is now two things, not one. {@code getUuid()} is what a type-CREATING core
 * reads off a freshly created type (a mapping OP's event factory reads nothing
 * else). The three PUBLIC FIELDS below — {@code fields}, {@code keyFields},
 * {@code fieldAlias} — are what a type-CONSUMING core reads off a RESOLVED source type, and
 * it reads them <b>reflectively</b>, by {@code getClass().getField("fields")} and friends
 * (e.g. an operator that re-types events). Reflection by name is why they must be fields with
 * exactly these names and not accessors: a getter would compile here and fail there.
 *
 * <p>
 * Matches the platform class's public surface:
 * {@code public List<String> keyFields}, {@code public Map<String,String> fields},
 * {@code public Map<String,String> fieldAlias}. Same names, same shapes, same public access.
 * A mock that drifts from that surface fails a core in the harness that works in production,
 * or worse, passes one that does not.
 */
public class MetaInfo {

    /**
     * The supertype every metadata object extends.
     *
     * <p>⚠ ADDED because its absence broke five cases outright:
     * {@code OpenProcessorCommon} began calling {@code MetaInfo.MetaObject} from
     * {@code AbstractReaderApp} and {@code MdrMetadataAccess}, and with no such inner class here
     * every op the harness constructed through that path died with
     * {@code NoClassDefFoundError: com/webaction/runtime/meta/MetaInfo$MetaObject} -- an error
     * naming the class but nothing about why an OP needed it.</p>
     *
     * <p><b>Shape matches the platform class</b>, like the rest of this
     * stub. The real class carries more fields
     * ({@code version}, {@code owner}, {@code ctime}, {@code coDependentObjects}, …); only what a
     * core actually reads is reproduced, which is this file's standing rule.</p>
     *
     * <p>{@code getFullName()} is {@code nsName + "." + name} on the real class -- plain
     * concatenation, which is why {@code AbstractReaderApp} notes that a
     * {@code getFullName() != null} guard provably cannot fire.</p>
     */
    public static class MetaObject {
        public String name;
        public String nsName;
        public String uri;
        public String description;
        public String metaObjectClass;
        public UUID uuid;
        public UUID namespaceId;

        public String getName() {
            return name;
        }

        public String getNsName() {
            return nsName;
        }

        public String getUri() {
            return uri;
        }

        public String getDescription() {
            return description;
        }

        /** {@code nsName + "." + name}, exactly as the real class computes it. */
        public String getFullName() {
            return nsName + "." + name;
        }
    }

    /**
     * ⚠ {@code extends MetaObject}, matching the real hierarchy
     * ({@code MetaInfo$Type extends MetaInfo$MetaObject}). {@code name} and {@code nsName} were
     * declared HERE before {@code MetaObject} existed; the comment that used to sit on them
     * already recorded that they belong to the supertype, so they now live where the real ones do
     * and a type-consuming core still reads them unchanged.
     */
    public static class Type extends MetaObject {
        private UUID uuid;

        /** Column name -> Java type name, in declaration order. */
        public Map<String, String> fields = new LinkedHashMap<>();
        /** Key column names; empty when the harness models none. */
        public List<String> keyFields = List.of();
        /** Column name -> display alias; empty when the harness models none. */
        public Map<String, String> fieldAlias = new LinkedHashMap<>();

        /**
         * ⚠ THE REAL CLASS'S ONLY CONSTRUCTOR, and this mock did not have it.
         *
         * <p>The platform's {@code MetaInfo$Type} declares
         * {@code Type()} and nothing else, populating itself through {@code construct(...)}
         * afterwards. This mock declared {@code Type(UUID)} and nothing else — exactly inverted.
         * The two constructor sets were therefore DISJOINT, and the consequence is not academic:
         * {@code src/main} code that compiles against the platform cannot construct a
         * {@code Type} the harness will accept, and vice versa. It cost MetricsReader's first
         * integration case a {@code NoSuchMethodError} from a {@code new MetaInfo.Type()} that
         * had been verified against the real jar and never against the mock that actually runs.
         *
         * <p>{@code getUuid()} answers {@code null} here, which is what the real class does after
         * a bare {@code new MetaInfo.Type()} — verified, along with {@code getFullName()}
         * returning {@code "null.null"} rather than throwing. A caller that needs identity sets
         * the public fields, as it must against the real class too.
         */
        public Type() {
        }

        /**
         * Harness-only convenience, kept because {@code IntegrationProcessor} builds types from a
         * case's {@code types:} block and already has the UUID in hand.
         *
         * <p><b>No equivalent exists on the real class</b>, so module code must never reach for
         * this shape — anything under {@code src/main} that did would fail to compile against the
         * platform. Retained rather than replaced so this mock's own two callers stay unchanged.
         */
        public Type(UUID uuid) {
            this.uuid = uuid;
        }

        public UUID getUuid() {
            return uuid;
        }
    }

    /**
     * ⚠ ADDED alongside {@link MetaObject}: {@code MdrMetadataAccess} CASTS a metadata object to
     * {@code (MetaInfo.Stream)}, so the type must exist for that path to load even when the
     * harness never populates one. Real shape: {@code MetaInfo$Stream extends
     * MetaInfo$MetaObject}, carrying {@code UUID dataType} among others.
     */
    public static class Stream extends MetaObject {
        public UUID dataType;
    }
}
