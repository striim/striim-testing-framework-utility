package com.example.common;

import java.util.Map;

import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * The type-system seam wrapping MDR/{@code Compiler}-backed type management: resolving
 * previously-registered types by UUID or name, and compiling + registering new ones.
 *
 * <p>This interface absorbs the type-management portion of the per-OP seams ({@code TypeResolver},
 * {@code PlatformContext}, {@code MdrPlatformContext}) into one interface with one
 * MDR/Compiler-backed production implementation, {@link MdrTypeResolver}. Field/alias
 * introspection and presence checking via {@code BuiltInFunc} is a separate concern; see
 * {@link BuiltInFuncs}.</p>
 *
 * <p>Production code constructs a {@link MdrTypeResolver}; tests inject a
 * {@code mock(TypeResolver.class)} — or, in the {@code scripts/integration} harness, a
 * {@code MockTypeResolver} — instead, with no live Striim server required.</p>
 */
public interface TypeResolver {

    /**
     * Resolves the type registered under {@code uuid}, or {@code null} if none exists — for
     * instance when the type was dropped since it was cached.
     */
    MetaInfo.Type getTypeByUUID(UUID uuid) throws Exception;

    /**
     * Resolves the type registered as {@code namespace.name}, or {@code null} if it does not exist
     * in the metadata repository.
     */
    MetaInfo.Type getTypeByName(String namespace, String name) throws Exception;

    /**
     * Compiles and registers a new Striim {@code Type}. Uses the generic {@code (typeName, maps)}
     * form — not an OP-specific Table-typed overload — so it stays reusable across every OP that
     * creates types.
     *
     * @param typeName     fully-qualified type name ({@code namespace.name})
     * @param fields       ordered field-name to Java type string
     * @param fieldAliases field-name to alias (proper casing); may be empty
     * @param fieldKeys    field-name to whether it is a key field
     * @return the newly-registered type, or {@code null} if creation failed
     */
    MetaInfo.Type createType(String typeName, Map<String, String> fields,
            Map<String, String> fieldAliases, Map<String, Boolean> fieldKeys) throws Exception;

    /**
     * Makes sure {@code type}'s generated class exists in THIS JVM.
     *
     * <p>A type outlives the JVM that compiled it: the MDR keeps the {@code Type}, the class lived
     * only in the dead JVM's {@code WALoader}. After a crash restart an adopted type therefore
     * resolves by name but {@code loadClass} fails, and every consumer that introspects the class
     * ({@code BuiltInFunc.getFieldsArray}) sees no fields. Measured 2026-09-20: JdbcSinkV1
     * halted on restart after a SIGKILL with "the source type ... reported no fields". The default
     * is a no-op for mocks; the production resolver regenerates the class when it is missing.</p>
     */
    default void ensureClass(MetaInfo.Type type) throws Exception {
    }
}
