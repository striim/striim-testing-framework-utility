package com.example.common;

import java.lang.reflect.Field;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

/**
 * The {@code BuiltInFunc}-backed "basic functions" seam: source-event field and alias
 * introspection, and presence checking. This is the platform coupling every OP needs for
 * event/field inspection — it does not touch the MDR/Compiler type system at all; see
 * {@link TypeResolver} for that separate concern.
 *
 * <p>Production code constructs a {@link BuiltInFuncResolver}; tests inject a
 * {@code mock(BuiltInFuncs.class)} — or, in the {@code scripts/integration} harness, a
 * {@code MockBuiltInFuncs} — instead, with no live Striim server required.</p>
 */
public interface BuiltInFuncs {

    /** Source-event field metadata (positional), by declared type UUID. */
    Field[] getFieldsArray(UUID sourceTypeUUID);

    /** Source-event field-name aliases {@code (event field name -> source column name)}. */
    Map<String, String> getAliasFieldName(UUID sourceTypeUUID);

    /** Whether the value at {@code index} is present (set) in the source image. */
    boolean IS_PRESENT(WAEvent event, Object[] image, int index);

    /**
     * Rebuilds the events a {@code WAEvent} JSON document describes.
     *
     * <p>On a SEAM because its failure modes must be scriptable — it is a plain Jackson
     * {@code readValue}, not a platform call. {@code ValueConversions} is final and static, so it
     * fails its own partition ("does a test need to substitute it").
     *
     * <p>The default THROWS rather than answering empty, which a caller could not tell from "the
     * document described none". ⚠ A Mockito mock bypasses it and answers an EMPTY LIST.
     *
     * @return the events the document describes; never null
     */
    default List<WAEvent> to_WAEvent(String json) throws Exception {
        throw new UnsupportedOperationException(
                getClass().getName() + " does not implement to_WAEvent");
    }
}
