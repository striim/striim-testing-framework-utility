package com.example.common;

import com.webaction.runtime.TypeGenerator;

/**
 * MDR type-<b>name</b> construction: where an OP builds the
 * {@code namespace.sourceName_table_Type} string a source-derived type registers under, and where
 * it takes that string apart again.
 *
 * <p><b>The shared implementation for resolving type names.</b> No production code outside this
 * class should call {@code TypeGenerator.getTypeName} directly.</p>
 *
 * <p><b>Deliberately not a method on {@link TypeResolver}, and not an interface.</b>
 * {@code TypeResolver} is the MDR/{@code Compiler} seam whose whole purpose is to be mocked; this
 * is a total function of three strings with exactly one correct answer. Putting it behind that
 * seam would actively break things, measured rather than assumed:
 *
 * <ul>
 *   <li>The integration harness resolves {@code TypeResolver} with a {@link java.lang.reflect.Proxy}
 *       ({@code IntegrationProcessor.newTypeResolverProxy}), and a Proxy routes <i>default</i>
 *       methods to its handler — whose fall-through is
 *       {@code throw new UnsupportedOperationException("MockTypeResolver." + name)}. Every adopting
 *       module would throw on its first event in the integration tier.</li>
 *   <li>A Mockito mock returns {@code null} for such a method — for an {@code abstract} one as
 *       readily as a {@code default} one — and a test may stub {@code createType} by
 *       matching the <i>exact expected type name</i>, so a null name would silently evaporate that
 *       test's premise rather than fail it.</li>
 * </ul>
 *
 * <p>Tests call this class directly, which is the point. That is not a compromise: the platform
 * function it wraps is already reachable offline — {@code Platform} is a {@code system}-scoped
 * dependency and therefore on the test classpath, and a module unit test in this repo has called it
 * directly for some time. Unlike {@code MDCache.getInstance()}, this static blocks no coverage.
 *
 * <p><b>Delegates; does not reimplement.</b> The exact string is a platform <i>contract</i>: these
 * names are registered in the MDR and referenced by {@code WAEvent.typeUUID}, so a private
 * reimplementation that drifted from Striim's would orphan every already-registered type. The
 * sanitiser behind it also depends on {@code Character.getName}, which is Unicode-version
 * dependent. {@code TypeNamesTest} pins the real jar's output, so a {@code STRIIM_VERSION} bump
 * that changed the scheme fails the build rather than production.
 */
public final class TypeNames {

    private TypeNames() {
    }

    /**
     * The fully-qualified MDR type name for one source-derived table:
     * {@code namespace + "." + sourceName + "_" + <table, sanitised> + "_Type"}.
     *
     * <p>{@code tableName} has its dots replaced with underscores and is then run through the
     * platform's Java-identifier sanitiser, so {@code "dbo.My Table"} becomes
     * {@code "dbo_My_SPACE_Table"} and a leading digit is spelled out
     * ({@code "1abc" -> "_DIGIT_ONE_abc"}). {@code namespace} and {@code sourceName} are
     * <b>not</b> sanitised — they are concatenated verbatim, which is why a {@code sourceName}
     * containing a dot produces a name with two of them (see {@link #simpleNameOf}).
     *
     * <p>The middle parameter is named {@code sourceName} after the platform's own signature, but
     * the platform imposes no meaning on it — it is a free discriminator between types that would
     * otherwise collide within one namespace, and a module may use it to carry a
     * logical namespace instead.
     *
     * @throws NullPointerException if {@code tableName} is null
     * @throws StringIndexOutOfBoundsException if {@code tableName} is empty
     *     — both are the platform's own behaviour, preserved rather than papered over, so that
     *     adopting this seam cannot change what a caller already experiences
     */
    public static String forTable(String namespace, String sourceName, String tableName) {
        return TypeGenerator.getTypeName(namespace, sourceName, tableName);
    }

    /**
     * The namespace segment of a name built by {@link #forTable} — everything before the
     * <b>first</b> dot, or the whole string when there is none.
     *
     * <p><b>First dot, not {@code split("\\.")[0]}.</b> The naive split is correct for the
     * namespace half but its sibling is not (see {@link #simpleNameOf}); both live here so a
     * caller cannot get one right and the other wrong.
     */
    public static String namespaceOf(String typeName) {
        int dot = typeName.indexOf('.');
        return dot < 0 ? typeName : typeName.substring(0, dot);
    }

    /**
     * The simple-name segment — everything after the <b>first</b> dot, or the whole string when
     * there is none.
     *
     * <p><b>This is the half that {@code split("\\.")[1]} gets wrong</b>, and modules
     * do exactly that when handing a name to {@code getMetaObjectByName}. That expression
     * silently truncates whenever the name carries a second dot — reachable, because
     * {@code sourceName} is concatenated unsanitised, so
     * {@code forTable("ns", "SRC.x", "T")} yields {@code ns.SRC.x_T_Type} whose {@code [1]} is the
     * bare {@code "SRC"} — and throws {@code ArrayIndexOutOfBoundsException} when it carries none.
     */
    public static String simpleNameOf(String typeName) {
        int dot = typeName.indexOf('.');
        return dot < 0 ? typeName : typeName.substring(dot + 1);
    }
}
