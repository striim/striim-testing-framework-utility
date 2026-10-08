package com.striim.testing.inttest;

import java.lang.reflect.Field;

/**
 * Supplies placeholder {@link Field} objects for the harness's mock {@code BuiltInFuncs.getFieldsArray}.
 * The real Striim {@code BuiltInFunc.getFieldsArray} returns the reflected fields of a generated
 * source-event class; {@code WAEventFactory} only reads each field's {@code getName()} and immediately
 * maps it through {@code getAliasFieldName} to the real source-column name — so these placeholders
 * (named {@code f0..f255}) never need to match a real column name; they are positional handles the
 * companion alias map (also built by the harness) renames. Capacity {@value #CAPACITY} columns.
 */
final class MockSourceFields {

    /**
     * ⚠ RAISED FROM 64 TO 256 for T3's 200-column perf workload (§55.4 asked for a very wide
     * table; §69.3 is the case). The cap is not a modelling choice — these are positional
     * placeholder handles an alias map renames, so the only cost of more is a wider class.
     * A schema beyond this still fails with the message that names this constant.
     */
    static final int CAPACITY = 256;

    // Positional placeholder fields f0..f255 (see class javadoc).
    public Object f0, f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13, f14, f15;
    public Object f16, f17, f18, f19, f20, f21, f22, f23, f24, f25, f26, f27, f28, f29, f30, f31;
    public Object f32, f33, f34, f35, f36, f37, f38, f39, f40, f41, f42, f43, f44, f45, f46, f47;
    public Object f48, f49, f50, f51, f52, f53, f54, f55, f56, f57, f58, f59, f60, f61, f62, f63;
    public Object f64, f65, f66, f67, f68, f69, f70, f71, f72, f73, f74, f75, f76, f77, f78, f79;
    public Object f80, f81, f82, f83, f84, f85, f86, f87, f88, f89, f90, f91, f92, f93, f94, f95;
    public Object f96, f97, f98, f99, f100, f101, f102, f103, f104, f105, f106, f107, f108, f109, f110, f111;
    public Object f112, f113, f114, f115, f116, f117, f118, f119, f120, f121, f122, f123, f124, f125, f126, f127;
    public Object f128, f129, f130, f131, f132, f133, f134, f135, f136, f137, f138, f139, f140, f141, f142, f143;
    public Object f144, f145, f146, f147, f148, f149, f150, f151, f152, f153, f154, f155, f156, f157, f158, f159;
    public Object f160, f161, f162, f163, f164, f165, f166, f167, f168, f169, f170, f171, f172, f173, f174, f175;
    public Object f176, f177, f178, f179, f180, f181, f182, f183, f184, f185, f186, f187, f188, f189, f190, f191;
    public Object f192, f193, f194, f195, f196, f197, f198, f199, f200, f201, f202, f203, f204, f205, f206, f207;
    public Object f208, f209, f210, f211, f212, f213, f214, f215, f216, f217, f218, f219, f220, f221, f222, f223;
    public Object f224, f225, f226, f227, f228, f229, f230, f231, f232, f233, f234, f235, f236, f237, f238, f239;
    public Object f240, f241, f242, f243, f244, f245, f246, f247, f248, f249, f250, f251, f252, f253, f254, f255;

    private MockSourceFields() {
    }

    /**
     * One placeholder {@link Field} per declared column, positional.
     *
     * <p>Shared by the OP path's injected {@code BuiltInFuncs} proxy and the target path's static
     * {@code BuiltInFunc}, so a case's {@code types:} block resolves to the same field array
     * whichever driver reads it. Two hand-written copies of this pairing is how the two would
     * disagree about a schema nobody changed.</p>
     */
    static Field[] fieldsFor(java.util.List<String> columns) {
        if (columns == null) {
            return new Field[0];
        }
        Field[] fields = new Field[columns.size()];
        for (int i = 0; i < fields.length; i++) {
            fields[i] = field(i);
        }
        return fields;
    }

    /** The companion alias map the placeholders are renamed through: {@code f<i> -> column}. */
    static java.util.Map<String, String> aliasesFor(java.util.List<String> columns) {
        java.util.Map<String, String> aliases = new java.util.HashMap<>();
        if (columns != null) {
            for (int i = 0; i < columns.size(); i++) {
                aliases.put("f" + i, columns.get(i));
            }
        }
        return aliases;
    }

    /** The placeholder field named {@code "f"+index}; throws if {@code index >= }{@value #CAPACITY}. */
    static Field field(int index) {
        if (index >= CAPACITY) {
            throw new IllegalArgumentException(
                    "types: source schema has more than " + CAPACITY + " columns (index " + index
                            + "); raise MockSourceFields.CAPACITY if a real fixture needs it");
        }
        try {
            return MockSourceFields.class.getDeclaredField("f" + index);
        } catch (NoSuchFieldException e) {
            throw new IllegalStateException("missing placeholder field f" + index, e);
        }
    }

    // ---- created types: fields named by column ----

    /** Field names of each type the mock TypeResolver created, by its UUID; one case per JVM. */
    private static final java.util.Map<Object, java.util.List<String>> CREATED =
            new java.util.concurrent.ConcurrentHashMap<>();
    private static final java.util.Map<java.util.List<String>, Field[]> NAMED =
            new java.util.concurrent.ConcurrentHashMap<>();

    /** Records a created type's field names, in declaration order, so its field array can be served. */
    static void registerCreated(Object uuid, java.util.List<String> fieldNames) {
        if (uuid != null && fieldNames != null) {
            CREATED.put(uuid, java.util.List.copyOf(fieldNames));
        }
    }

    /** A created type's field names, or null for a type the mock did not create. */
    static java.util.List<String> createdFields(Object uuid) {
        return uuid == null ? null : CREATED.get(uuid);
    }

    /**
     * Real {@link Field}s named by {@code names}, in that order -- what a platform-generated type
     * class reflects, and what a core that proves a created type's field order compares against.
     * Compiled once per name list from a generated class with one public field per name. Returns
     * null when that is impossible here (no JDK compiler, or a name that is not a Java
     * identifier), so the caller falls back to what it served before.
     */
    static Field[] namedFields(java.util.List<String> names) {
        if (names == null || names.isEmpty()) {
            return null;
        }
        for (String n : names) {
            if (!javax.lang.model.SourceVersion.isName(n)) {
                return null;
            }
        }
        return NAMED.computeIfAbsent(java.util.List.copyOf(names), MockSourceFields::compileNamed);
    }

    private static Field[] compileNamed(java.util.List<String> names) {
        javax.tools.JavaCompiler compiler = javax.tools.ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            return null;
        }
        try {
            java.nio.file.Path dir = java.nio.file.Files.createTempDirectory("inttest-type-");
            String cls = "CreatedType" + Integer.toHexString(names.hashCode());
            StringBuilder src = new StringBuilder("public class ").append(cls).append(" {\n");
            for (String n : names) {
                src.append("    public Object ").append(n).append(";\n");
            }
            src.append("}\n");
            java.nio.file.Path file = dir.resolve(cls + ".java");
            java.nio.file.Files.writeString(file, src);
            if (compiler.run(null, null, null, "-d", dir.toString(), file.toString()) != 0) {
                return null;
            }
            Class<?> type = new java.net.URLClassLoader(new java.net.URL[] { dir.toUri().toURL() },
                    MockSourceFields.class.getClassLoader()).loadClass(cls);
            Field[] fields = new Field[names.size()];
            for (int i = 0; i < fields.length; i++) {
                fields[i] = type.getField(names.get(i));
            }
            return fields;
        } catch (Exception e) {
            return null;
        }
    }
}
