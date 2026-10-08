package com.example.common;

import java.util.Map;

/**
 * Reads a TQL property off the operator's {@code Map<String, Object>}, coercing it to a
 * {@code String}, {@code int} or {@code boolean} and falling back to a documented default.
 *
 * <p><b>This is a HOIST of code several modules already had, not a new idea.</b> Readers
 * declared {@code strProp}/{@code intProp}/{@code boolProp} privately in their {@code Processor}
 * or {@code Config}. The flat-property shape is what every reader takes — {@link ConfigParser} and
 * {@link ConfigVerifier} are a different thing entirely, for the modules whose config arrives as a
 * JSON FILE.</p>
 *
 * <p>⚠ <b>A test fixture that must fail loudly should NOT adopt this class.</b> A fixture whose
 * property reader trims, treats a blank as absent, and <b>throws</b> on both a malformed integer
 * and an unrecognised boolean spelling is deliberately STRICTER: a case writing {@code 'Y'} for a
 * boolean must fail, where {@link #bool} would read it as {@code false} and silently run the
 * opposite scenario.</p>
 *
 * <p><b>The copies had already diverged, which is the argument for one of them existing.</b>
 * {@link Map#getOrDefault} returns its default only when the key is ABSENT — a key present with a
 * {@code null} value returns {@code null}. Most copies guarded that everywhere; one guarded it in
 * its string reader and NOT in its {@code int} and {@code boolean} ones, and those then called
 * {@code v.toString()} on {@code null} and threw a {@code NullPointerException} out of the
 * constructor. Returning the documented default is what the contract says, so <b>this class
 * guards</b> — which means adopting it FIXES such a module rather than preserving it.</p>
 *
 * <p><b>What this deliberately does NOT hoist</b>, because the modules it serves do not do
 * it and a shared layer reproduces the behaviour it absorbs rather than improving it in the same
 * change: trimming every value, a NAMED {@code IllegalArgumentException} for a malformed integer
 * rather than a bare {@code NumberFormatException}, and range-checked variants. Folding them in
 * here would silently alter how existing readers treat a padded or malformed value.</p>
 *
 * <p>🚨 <b>The case with teeth is the BLANK value, and an adopting change must decide it
 * first.</b> {@link #integer} throws {@link NumberFormatException} on {@code ""}. A module whose
 * own parser returns the default for an empty string — so a TQL property left blank, such as
 * {@code RepeatInSeconds: ''}, takes the documented default today — would, on adopting this class
 * without deciding the case, turn an app that deploys today into one that fails out of its
 * constructor.</p>
 *
 * <p><b>The key is matched ignoring case, because that is what the platform's
 * map does.</b> The map an adapter receives is {@code Compiler.combineProperties}'
 * {@code TreeMap(String.CASE_INSENSITIVE_ORDER)}, so on a node {@code get("EnableLogging")}
 * already finds {@code enablelogging:}. A unit test's {@code HashMap} does not, and a reader
 * that only matched exactly passed there while behaving differently on the node. The exact key
 * is tried first and wins; a case variant is found by one scan of the entries, which only ever
 * runs on a map that is not the platform's.</p>
 */
public final class Props {

    private Props() {
    }

    /**
     * The property as text, or {@code defaultValue} when the key is absent or maps to
     * {@code null}.
     */
    public static String str(Map<String, Object> properties, String key, String defaultValue) {
        Object v = value(properties, key);
        return (v == null) ? defaultValue : v.toString();
    }

    /**
     * The property as an {@code int}, or {@code defaultValue} when the key is absent or maps to
     * {@code null}.
     *
     * <p>An {@code Integer} is taken as-is; anything else is parsed from its text, so the
     * {@code String} form a TQL property usually arrives in works. <b>A malformed value throws
     * {@link NumberFormatException} rather than falling back</b> — silently substituting a default
     * would run the operator with a setting nobody chose.</p>
     */
    public static int integer(Map<String, Object> properties, String key, int defaultValue) {
        Object v = value(properties, key);
        if (v == null) {
            return defaultValue;
        }
        return (v instanceof Integer) ? (Integer) v : Integer.parseInt(v.toString());
    }

    /**
     * The property as a {@code boolean}, or {@code defaultValue} when the key is absent or maps to
     * {@code null}.
     *
     * <p>Follows {@link Boolean#parseBoolean}: anything that is not {@code "true"}, ignoring case,
     * is {@code false}. That is the behaviour all three hoisted copies had, and it is why this
     * method cannot report a typo the way {@link #integer} does.</p>
     */
    public static boolean bool(Map<String, Object> properties, String key, boolean defaultValue) {
        Object v = value(properties, key);
        if (v == null) {
            return defaultValue;
        }
        return (v instanceof Boolean) ? (Boolean) v : Boolean.parseBoolean(v.toString());
    }

    /**
     * The raw mapped value, or {@code null} for both "absent" and "present but null" — the two
     * cases every caller here treats alike, and the distinction the {@code getOrDefault} form
     * these copies used got wrong.
     *
     * <p>A {@code null} {@code properties} map answers {@code null} rather than throwing, so every
     * reader here falls back. <b>That is a guarantee about THIS class, not about any operator</b>:
     * an adopter is free to touch the map itself before it ever calls one of these, and one that
     * does will still throw on {@code null}. The point is only that this class is not the thing
     * that throws. Deliberately names no adopter: the previous wording cited one by module and
     * method, and went stale the moment that module adopted {@link #str}.</p>
     */
    private static Object value(Map<String, Object> properties, String key) {
        if (properties == null || key == null) {
            return null;
        }
        Object v = properties.get(key);
        if (v != null || properties.containsKey(key)) {
            return v;
        }
        for (Map.Entry<String, Object> e : properties.entrySet()) {
            if (key.equalsIgnoreCase(e.getKey())) {
                return e.getValue();
            }
        }
        return null;
    }
}
