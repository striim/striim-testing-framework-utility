package com.example.common;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import com.webaction.metaRepository.MetadataRepository;
import com.webaction.runtime.Context;
import com.webaction.runtime.compiler.Compiler;
import com.webaction.runtime.compiler.TypeDefOrName;
import com.webaction.runtime.compiler.TypeField;
import com.webaction.runtime.compiler.TypeName;
import com.webaction.runtime.compiler.stmts.CreateTypeStmt;
import com.webaction.runtime.components.EntityType;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.AuthToken;
import com.webaction.uuid.UUID;

/**
 * Production {@link TypeResolver}: the single MDR/{@code Compiler}-backed implementation of the
 * type-management seam: {@link #getTypeByUUID} and {@link #getTypeByName} against
 * {@link MetadataRepository}, and {@link #createType} through the runtime {@link Compiler}.
 *
 * <p>The security token is fetched lazily on first use, not at construction, via the shared
 * {@link SecurityTokenProvider} — the platform's {@code MDCache} is not guaranteed initialized at
 * OP construction time.</p>
 *
 * <p>Not unit tested here; it exists precisely to isolate live-server calls, and tests inject a
 * {@code mock(TypeResolver.class)} instead. A compile-and-construction smoke lives in this module's
 * test tree; full behavioural coverage comes from the {@code scripts/integration} harness's
 * {@code MockTypeResolver}.</p>
 */
public class MdrTypeResolver implements TypeResolver {

    private final Logger logger;
    private final SecurityTokenProvider tokens = new SecurityTokenProvider();

    /** Production constructor: no eager MDCache/token access. */
    public MdrTypeResolver() {
        this(null);
    }

    /** Optional logger for createType's diagnostic tracing, mirroring MdrPlatformContext. */
    public MdrTypeResolver(Logger logger) {
        this.logger = logger;
    }

    /** Lazily resolves and caches the security-manager token. */
    private AuthToken token() {
        return tokens.token();
    }

    private void log(java.util.function.Supplier<String> msg) {
        if (logger != null) {
            logger.log(msg);
        }
    }

    /** Resolves the type registered under {@code uuid}, or {@code null} if none exists. */
    @Override
    public MetaInfo.Type getTypeByUUID(UUID uuid) throws Exception {
        return (MetaInfo.Type) MetadataRepository.getINSTANCE().getMetaObjectByUUID(uuid, token());
    }

    /**
     * Resolves the type registered as {@code namespace.name}, or {@code null} if it does not exist
     * in the metadata repository.
     */
    @Override
    public MetaInfo.Type getTypeByName(String namespace, String name) throws Exception {
        return (MetaInfo.Type) MetadataRepository.getINSTANCE()
                .getMetaObjectByName(EntityType.TYPE, namespace, name, 1, token());
    }

    /** {@inheritDoc} {@code MetaInfo.Type.generateClass} is idempotent: a class already resident is kept. */
    @Override
    public void ensureClass(MetaInfo.Type type) throws Exception {
        if (type != null) {
            type.generateClass();
        }
    }

    /**
     * Compiles and registers a new Striim {@code Type}: builds a {@code TypeField} per field
     * (name + Java type + key flag), sets the alias via reflection when one is supplied, then
     * drives the runtime {@code Compiler} and resolves the just-created type by its compiled UUID.
     */
    @Override
    public MetaInfo.Type createType(String typeName, Map<String, String> fields,
            Map<String, String> fieldAliases, Map<String, Boolean> fieldKeys) throws Exception {

        // The choke point for every module that mints a type, whether or not it came through
        // TypeSchemas.getOrCreate.
        TypeSchemas.requireCreatableFieldNames(typeName, fields.keySet());

        log(() -> "createType: Creating new type: " + typeName);
        log(() -> "createType: Field aliases: " + fieldAliases);
        log(() -> "createType: Field keys: " + fieldKeys);

        final List<TypeField> typeFields = new ArrayList<>();
        final List<String> keyFieldsList = new ArrayList<>();

        for (final String fieldName : fields.keySet()) {
            final String fieldType = fields.get(fieldName);
            final TypeName newtype = new TypeName(fieldType, 0);
            final Boolean isKey = fieldKeys.getOrDefault(fieldName, false);
            final String alias = fieldAliases.get(fieldName);

            log(() -> "createType: Creating TypeField with name='" + fieldName + "', alias='" + alias
                    + "', type='" + fieldType + "', isKey=" + isKey);

            TypeField col = new TypeField(fieldName, newtype, isKey);

            if (isKey) {
                keyFieldsList.add(fieldName);
                log(() -> "createType: Added '" + fieldName + "' to keyFieldsList");
            }

            if (alias != null && !alias.isEmpty()) {
                try {
                    java.lang.reflect.Field aliasField = col.getClass().getField("fieldAlias");
                    aliasField.set(col, alias);
                    log(() -> "createType: Set fieldAlias to '" + alias + "' for field '" + fieldName + "'");
                } catch (Exception e) {
                    log(() -> "createType: Could not set fieldAlias: " + e.getMessage());
                }
            }

            typeFields.add(col);
        }

        log(() -> "createType: Key fields list for new type: " + keyFieldsList);

        final AuthToken myToken = token();
        final TypeDefOrName typeDef = new TypeDefOrName(typeName, typeFields);
        // doReplace=true.
        //
        // This method does not check whether a live type of this name already exists, and it may
        // not assume its caller did: it is the write primitive, and a caller that skips the check
        // would find nothing here to stop it.
        //
        // Both obvious repairs are wrong, in opposite directions:
        //   - "look it up and return the existing type": a name match does NOT prove a schema
        //     match (the derived name keeps only a prefix per column), and adopting a
        //     differently-shaped type stamps this app's data[] with that type's UUID, landing
        //     values under the wrong column names. Silent corruption.
        //   - "keep replacing" clobbers another app's live type.
        // The right answer is: look up, VERIFY the schema, adopt or refuse loudly. That is
        // {@link TypeSchemas}. Every caller on this seam either goes through
        // TypeSchemas.getOrCreate or runs its own getTypeByName + firstMismatch check first, so by
        // the time control reaches here the shape HAS been compared and the caller's policy has
        // decided to write.
        //
        // What doReplace decides is the LIVE-type case: false throws "already exists", true
        // replaces. It is not needed for a dropped type whose generated class is still resident in
        // the classloader; the platform handles that case the same either way.
        final CreateTypeStmt ctStmt = new CreateTypeStmt(typeName, true, typeDef);
        final BuiltInFuncTypeCompileCallback cb = new BuiltInFuncTypeCompileCallback(ctStmt);

        final Context ctx = Context.createContext(myToken);
        Compiler.compile(ctStmt, ctx, cb);

        MetaInfo.Type type = (MetaInfo.Type) MetadataRepository.getINSTANCE()
                .getMetaObjectByUUID(cb.uuid, myToken);

        if (type != null) {
            try {
                java.lang.reflect.Field keyFieldsField = type.getClass().getField("keyFields");
                @SuppressWarnings("unchecked")
                List<String> actualKeyFields = (List<String>) keyFieldsField.get(type);
                log(() -> "createType: Created type has keyFields: " + actualKeyFields);
            } catch (Exception e) {
                log(() -> "createType: Could not verify keyFields: " + e.getMessage());
            }
        }

        return type;
    }
}
