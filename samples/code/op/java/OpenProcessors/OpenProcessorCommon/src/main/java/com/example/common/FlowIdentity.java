package com.example.common;

/**
 * The pure half of flow identity: turning a namespace and name pair from the platform's metadata
 * into a qualified name, or into {@code null} when the platform cannot answer yet.
 *
 * <p><b>Why this is a separate, platform-free class.</b> Not because the platform reach cannot be
 * tested — an earlier version of this javadoc said exactly that, and it was wrong. It claimed a
 * {@code SourceProcess} subclass "cannot be constructed outside a live server, so no fake can drive
 * it"; the {@code NoClassDefFoundError} behind that claim was one missing {@code jeromq} line in
 * this module's pom, and {@link AbstractReaderApp}'s accessors can be driven for real in a
 * test.</p>
 *
 * <p>The real reason stands on its own: <b>this is the half where hand-rolled copies
 * actually diverged.</b> One returned the component's own name where another returned {@code null},
 * in checkpoint-identity code. A decision that subtle deserves tests that name each case, and pure
 * string logic can have them cheaply — the platform hop around it is a single unbranched call per
 * value, which is a different kind of risk.</p>
 *
 * <p><b>Compose from PARTS — there is deliberately no joined-name entry point.</b> The platform's
 * {@code MetaInfo.MetaObject.getFullName()} is literally {@code nsName + "." + name}: plain string
 * concatenation with no null handling. It therefore cannot return {@code null} or {@code ""}, and a
 * missing part surfaces as the literal text {@code "null"} inside an otherwise well-formed name —
 * {@code "null.null"}, {@code "null.MySource"}. A hand-roll guarding with
 * {@code fullName != null && !fullName.isEmpty()} <b>provably cannot fire</b> and lets those
 * degenerate values through as if they were real identities. Reading {@code getNsName()} and
 * {@code getName()} separately makes the degenerate case structurally unrepresentable instead of
 * something to sniff for.</p>
 *
 * @see AbstractReaderApp for the reader-side reach, and {@code AbstractOpenProcessorApp} for the
 *      in-stream one
 */
public final class FlowIdentity {

    /** The separator the platform itself uses in {@code getFullName()}. */
    public static final String SEPARATOR = ".";

    private FlowIdentity() {
    }

    /**
     * {@code nsName.name}, or {@code null} if either part is absent.
     *
     * <p>{@code null} means "the platform cannot answer this yet" and callers must treat it as a
     * reason to <b>defer</b>, never as a value to substitute for. A caller that latches a
     * placeholder derived from {@code null} pins it for the life of the process — which is how a
     * per-component checkpoint filename degrades into one shared by every reader on the node.</p>
     *
     * <p>Blank parts count as absent: the platform stores an unset namespace as {@code null}, but a
     * whitespace-only value is not a usable identity either and must not become one by
     * concatenation.</p>
     */
    public static String compose(String nsName, String name) {
        if (isBlank(nsName) || isBlank(name)) {
            return null;
        }
        return nsName.trim() + SEPARATOR + name.trim();
    }

    /**
     * Whether {@code part} is a usable half of a qualified name.
     *
     * <p><b>No literal-{@code "null"} check, deliberately.</b> An earlier revision rejected the
     * four-character string too, on the grounds that it is what {@code nsName + "." + name} leaves
     * behind for an absent part. That was reachable only from a {@code normalize(joinedName)}
     * helper which had no production caller and has since been deleted — every caller here passes
     * the platform's {@code getNsName()} and {@code getName()} directly, where an absent part
     * arrives as a Java {@code null} and is caught by the first clause. Composing from parts is
     * what makes the degenerate form unrepresentable; a string check was belt-and-braces for a path
     * that does not exist.</p>
     */
    private static boolean isBlank(String part) {
        return part == null || part.trim().isEmpty();
    }
}
