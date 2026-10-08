package com.striim.testing.inttest;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * The {@code types:} linkage (README "Test Layout"): one minted {@code typeUUID} per declared
 * source table, the columns behind it, and the stamping of that UUID onto matching input events.
 *
 * <p>Extracted when the target driver arrived, for the reason {@link LoadedOp}'s own javadoc gives
 * about itself: a second hand-written copy of this is how two drivers quietly diverge. Both
 * {@link LoadedOp} (OpenProcessors, UDFs, readers) and {@link TargetCore} (targets) build the
 * linkage from here, so a case's {@code types:} block means the same thing whichever it drives.</p>
 *
 * <p><b>An OP and a target reach it by different routes, and that is the point.</b> An OP is HANDED
 * a {@code BuiltInFuncs} proxy by constructor injection. A target is constructed by the platform's
 * no-arg constructor and builds its own {@code BuiltInFuncResolver}, which calls the STATIC
 * {@code com.webaction.runtime.BuiltInFunc}. So a target case exercises the production resolver,
 * not a substitute for it — so a source type's helper field must not be counted as a column.</p>
 */
final class SourceTypes {

    /** Ordered source columns by minted type UUID. */
    final Map<UUID, List<String>> columnsByUuid = new LinkedHashMap<>();
    /** Minted type UUID by source {@code metadata.TableName}. */
    final Map<String, UUID> uuidByTable = new LinkedHashMap<>();
    /** The declared source type by minted UUID, for the {@code TypeResolver} seam. */
    final Map<UUID, MetaInfo.Type> declaredByUuid = new LinkedHashMap<>();
    /** The declared source type by table name, for the {@code TypeResolver} seam. */
    final Map<String, MetaInfo.Type> declaredByName = new LinkedHashMap<>();

    private SourceTypes() {
    }

    /**
     * Mints a UUID per declared table and builds its source type.
     *
     * <p>{@code time=0} so a schema UUID can never collide with a reader-stamped
     * {@code UUID.genCurTimeUUID()}, whose time is the wall clock.</p>
     */
    static SourceTypes from(Map<String, Object> types) {
        SourceTypes resolved = new SourceTypes();
        long counter = 0;
        for (Map.Entry<String, List<String>> entry
                : IntegrationProcessor.parseTypeSchemas(types).entrySet()) {
            UUID uuid = new UUID(0L, ++counter);
            String table = entry.getKey();
            resolved.uuidByTable.put(table, uuid);
            resolved.columnsByUuid.put(uuid, entry.getValue());

            Object spec = types == null ? null : types.get(table);
            MetaInfo.Type declared = IntegrationProcessor.newSourceType(uuid, entry.getValue(),
                    table, IntegrationProcessor.keysOf(spec), IntegrationProcessor.aliasesOf(spec));
            resolved.declaredByUuid.put(uuid, declared);
            resolved.declaredByName.put(table, declared);
        }
        return resolved;
    }

    /** Stamps each event whose {@code metadata.TableName} names a declared table with its UUID. */
    void stamp(List<? extends com.webaction.event.Event> events) {
        for (com.webaction.event.Event raw : events) {
            // Only a WAEvent carries a typeUUID to stamp. An operator whose input is another
            // event kind (an AvroEvent) resolves types through its own TypeResolver seam instead,
            // so there is nothing here to link -- skip rather than reject, so a `types:` block
            // declared for such a case's OUTPUT expectations is still allowed to exist.
            if (!(raw instanceof WAEvent event)) {
                continue;
            }
            if (event.metadata == null) {
                continue;
            }
            Object tableName = event.metadata.get("TableName");
            if (tableName == null) {
                continue;
            }
            UUID uuid = uuidByTable.get(tableName.toString());
            if (uuid != null) {
                event.typeUUID = uuid;
            }
        }
    }
}
