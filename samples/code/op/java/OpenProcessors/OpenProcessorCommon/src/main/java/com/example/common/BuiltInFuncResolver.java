package com.example.common;

import java.lang.reflect.Field;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import com.webaction.metaRepository.MetadataRepository;
import com.webaction.runtime.BuiltInFunc;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * Production {@link BuiltInFuncs}: a thin, stateless wrapper over the static {@link BuiltInFunc}
 * methods used for source-event field and alias introspection and presence checking:
 * {@link #getFieldsArray}, {@link #getAliasFieldName} and {@link #IS_PRESENT}.
 *
 * <p>{@code BuiltInFunc}'s statics need no token; the one MDR lookup here (a class missing after
 * a crash restart, see {@link #getFieldsArray}) fetches it lazily. Compare {@link MdrTypeResolver}.</p>
 *
 * <p>Not unit tested here beyond a compile-and-construction smoke, since it exists precisely to
 * isolate live-server calls; tests inject a {@code mock(BuiltInFuncs.class)} instead. Full
 * behavioural coverage comes from ReferenceOp and the {@code scripts/integration} harness's
 * {@code MockBuiltInFuncs}.</p>
 */
public class BuiltInFuncResolver implements BuiltInFuncs {

    private final SecurityTokenProvider tokens = new SecurityTokenProvider();

    /** Source-event field metadata (positional), by declared type UUID. */
    @Override
    public Field[] getFieldsArray(UUID sourceTypeUUID) {
        final Field[] fields = BuiltInFunc.getFieldsArray(sourceTypeUUID);
        if (fields != null || sourceTypeUUID == null) {
            return fields;
        }
        // Null with a UUID means the platform found the type but could not load its class: the
        // class lived only in a JVM that is gone (a restart after a crash) while the MDR kept the
        // type. An event replayed from a persisted stream carries that UUID. Regenerate the class
        // and ask once more; a type the MDR does not know stays null.
        final MetaInfo.Type type;
        try {
            type = (MetaInfo.Type) MetadataRepository.getINSTANCE()
                    .getMetaObjectByUUID(sourceTypeUUID, tokens.token());
        } catch (final Exception e) {
            return null;
        }
        if (type == null) {
            return null;
        }
        try {
            type.generateClass();
        } catch (final Exception e) {
            // The type exists and its class cannot be built: the cause is what the operator
            // needs, not the caller's "reported no fields".
            throw new IllegalStateException("the class for type " + type.getFullName()
                    + " could not be regenerated: " + e.getMessage(), e);
        }
        return BuiltInFunc.getFieldsArray(sourceTypeUUID);
    }

    /** Source-event field-name aliases {@code (event field name -> source column name)}. */
    @Override
    public Map<String, String> getAliasFieldName(UUID sourceTypeUUID) {
        return BuiltInFunc.getAliasFieldName(sourceTypeUUID);
    }

    /**
     * {@inheritDoc}
     *
     * <p>🚨 <b>A consumer calling this needs {@code jackson-datatype-joda} and
     * {@code jackson-datatype-jsr310} in ITS OWN pom.</b> {@code ObjectMapperFactory} registers both
     * modules, and THIS module's pom cannot supply them: {@code OpenProcessorCommon} is consumed by
     * {@code add-source}, so only its SOURCE reaches a consumer, never its dependencies — the same
     * trap encountered with {@code org.json}. ReferenceOp does not declare them. It fails at
     * RUNTIME, so the build does not catch it.</p>
     */
    @Override
    public java.util.List<com.webaction.proc.events.WAEvent> to_WAEvent(String json) throws Exception {
        return BuiltInFunc.to_WAEvent(json);
    }

    /** Whether the value at {@code index} is present (set) in the source image. */
    @Override
    public boolean IS_PRESENT(WAEvent event, Object[] image, int index) {
        return BuiltInFunc.IS_PRESENT(event, image, index);
    }
}
