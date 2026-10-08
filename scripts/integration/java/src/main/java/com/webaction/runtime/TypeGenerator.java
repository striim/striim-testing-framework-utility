package com.webaction.runtime;

/**
 * Minimal mock of Striim's {@code com.webaction.runtime.TypeGenerator}. Only the pure,
 * offline static {@code getTypeName(namespace, sourceName, tableName)} an operator under test
 * calls to build a target type NAME is reproduced. The exact string form need not match Striim's
 * real scheme: the harness never emits {@code typeUUID}, so a target type name is never compared —
 * it only has to be deterministic and non-null so the operator does not NPE building its type.
 */
public final class TypeGenerator {

    private TypeGenerator() {
    }

    public static String getTypeName(String namespace, String sourceName, String tableName) {
        return namespace + "." + sourceName + "_" + String.valueOf(tableName).replace('.', '_') + "_Type";
    }
}
