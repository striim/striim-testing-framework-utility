package com.example.common;

import com.webaction.anno.PropertyTemplate;
import com.webaction.anno.PropertyTemplateProperty;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Which TQL properties an App DECLARES, and which of the ones it RECEIVED it never declared.
 *
 * <p><b>Why this exists.</b> Striim does not reject an unknown property in a
 * {@code CREATE … USING (…)} clause (observed on 5.4). So a typo'd or retired property deploys
 * clean, the operator silently reverts to its default, and the application runs — differently —
 * indefinitely.</p>
 *
 * <p>It matters most on upgrade, because <b>every OP version bump is a TQL edit</b>, which is
 * exactly when a property name is mistyped or forgotten.</p>
 *
 * <p><b>REPORTING, NOT REFUSING — deliberately, and this is the decision to revisit.</b> Failing
 * startup was considered. This reports instead, because refusing is a behaviour change
 * that would turn a silently-wrong app into a dead one on upgrade — for a
 * property the platform itself accepted. Promote it once OPs have run with the report and the
 * false-positive rate is known to be zero.</p>
 *
 * <p><b>It reports at ERROR, and that is deliberate rather than an inconsistency with "does not
 * refuse".</b> {@link Logger} reserves {@code logWarn} for "a tolerated one" and {@code logError}
 * for a genuine failure that should trip alerting. An adapter running on a default its TQL did not
 * ask for IS a genuine failure — it is the silent-migration class this guard exists for. What it
 * does not do is stop the app.</p>
 *
 * <p><b>Coverage is every shell.</b> The in-stream shell reports from
 * {@code start()}, {@link AbstractReaderApp} from its {@code init(Map)} — which every
 * {@code BaseProcess.init} overload chains down to — and {@link AbstractWriterApp} from its
 * {@code init}. A module extending {@code SourceProcess} directly gets nothing.
 * Verified on a live node: a reader with a Password property, {@code UUID} and {@code adapterName}
 * in its map reports nothing; the in-stream guard's {@code Password_encrypted} false positive is
 * gone.</p>
 *
 * <p><b>Names are compared ignoring case, because the platform does.</b> The
 * map an adapter receives is {@code Compiler.combineProperties}' {@code TreeMap} with
 * {@code String.CASE_INSENSITIVE_ORDER}, and it survives MDR persistence: probed live,
 * a lower-case property name in TQL reached the adapter's mixed-case property. So a case
 * difference is not a typo the adapter is running without — it is honoured — and reporting it (refusing, under
 * strict) would be a false positive. {@link Props} folds case the same way so a hand-built map
 * in a unit test behaves like the node's.</p>
 */
public final class DeclaredProperties {

    /**
     * The opt-in refusal. {@code StrictProperties: 'true'} in any sample adapter's
     * {@code USING (...)} clause turns the report into a startup failure naming every undeclared
     * key. It is not in any {@code @PropertyTemplate}: this guard understands it for every
     * adapter that reaches it, and Striim accepts it like any other property. Off by default,
     * so an existing application keeps starting; a customer turns it on for a version-bump
     * deploy and off after.
     */
    public static final String STRICT_PROPERTY = "StrictProperties";

    /**
     * The node-wide default for {@link #STRICT_PROPERTY}: {@code -Dcom.example.strictProperties=true}
     * on the server JVM makes every sample adapter on that node strict unless its own TQL says
     * {@code StrictProperties: 'false'}. The TQL property wins in both directions.
     */
    public static final String STRICT_SYSTEM_PROPERTY = "com.example.strictProperties";

    /**
     * Reserved keys the shell injects that are NOT {@code @PropertyTemplate} properties, so they
     * must never be reported as undeclared.
     *
     * @see AbstractConvertingOpenProcessorApp#NAMESPACE_KEY
     */
    private static final Set<String> RESERVED = Set.of(
            AbstractConvertingOpenProcessorApp.NAMESPACE_KEY,
            AbstractConvertingOpenProcessorApp.SOURCE_NAME_KEY,
            // Understood by this guard itself, so it is never a finding.
            STRICT_PROPERTY,
            // The platform's own injections (5.4):
            // BaseProcess.init and SourceProcess.init put "UUID"; the compiler puts "adapterName"
            // on every source and target; SourceProcess.init puts "TABLESEXTENDEDPROPERTY" when a
            // Tables property carries the extended syntax.
            "UUID", "adapterName", "TABLESEXTENDEDPROPERTY");

    /**
     * The compiler's companion for a Password-typed property: {@code <key>_encrypted=true}
     * (the compiler adds it). Derived, never typed by the operator,
     * so it is never a finding of its own — and it made the in-stream guard report
     * {@code Password_encrypted} on every start before excluding platform-injected properties.
     */
    private static final String ENCRYPTED_SUFFIX = "_encrypted";


    private DeclaredProperties() {
    }

    /**
     * The property names {@code appClass} declares, or an empty set when it carries no
     * {@code @PropertyTemplate}.
     *
     * <p>Empty means "cannot tell", not "declares nothing" — {@link #undeclared} treats it that
     * way and reports nothing, because an App without the annotation is a shape this cannot
     * reason about rather than one with zero properties.</p>
     */
    public static Set<String> declaredBy(Class<?> appClass) {
        Set<String> names = new LinkedHashSet<>();
        if (appClass == null) {
            return names;
        }
        PropertyTemplate template = appClass.getAnnotation(PropertyTemplate.class);
        if (template == null) {
            return names;
        }
        for (PropertyTemplateProperty p : template.properties()) {
            if (p.name() != null && !p.name().trim().isEmpty()) {
                names.add(p.name().trim());
            }
        }
        return names;
    }

    /**
     * Received keys that {@code appClass} never declared, in encounter order.
     *
     * <p>Empty when the App carries no {@code @PropertyTemplate} — see {@link #declaredBy}.
     * Comparison ignores case, as the platform's map does (see the class comment): the earlier
     * exact comparison reported {@code enablelogging} for {@code EnableLogging} as undeclared
     * while the adapter was in fact reading it.</p>
     */
    public static List<String> undeclared(Class<?> appClass, Map<String, Object> received) {
        List<String> unknown = new ArrayList<>();
        if (received == null || received.isEmpty()) {
            return unknown;
        }
        Set<String> declared = declaredBy(appClass);
        if (declared.isEmpty()) {
            return unknown;                 // cannot tell; say nothing rather than accuse
        }
        Set<String> accepted = new TreeSet<>(String.CASE_INSENSITIVE_ORDER);
        accepted.addAll(declared);
        accepted.addAll(RESERVED);
        for (String key : received.keySet()) {
            if (key == null || accepted.contains(key)) {
                continue;
            }
            if (isEncryptedCompanion(key)) {
                continue;   // its base is the finding, if there is one
            }
            unknown.add(key);
        }
        return unknown;
    }

    /** {@code <base>_encrypted} in any case: the platform compares the companion ignoring case. */
    private static boolean isEncryptedCompanion(String key) {
        return key.length() > ENCRYPTED_SUFFIX.length()
                && key.regionMatches(true, key.length() - ENCRYPTED_SUFFIX.length(),
                        ENCRYPTED_SUFFIX, 0, ENCRYPTED_SUFFIX.length());
    }

    /**
     * Logs one ERROR per undeclared key, with a did-you-mean suggestion where one is close, and
     * returns the keys reported. Every shell calls this from its {@code init}/{@code start}:
     * the in-stream shell, {@link AbstractReaderApp} and {@link AbstractWriterApp}.
     *
     * <p>The check itself never throws — a diagnostic must not be the thing that stops an app
     * starting. The one exception is the one the operator asked for: under
     * {@link #STRICT_PROPERTY} (or the {@link #STRICT_SYSTEM_PROPERTY} default) an undeclared key
     * is an {@link IllegalStateException} naming every such key, after the per-key lines have
     * been logged, so the app fails to start instead of running on a default it did not ask for.
     */
    public static List<String> report(Class<?> appClass, Map<String, Object> received, Logger logger) {
        List<String> unknown;
        try {
            unknown = undeclared(appClass, received);
            if (unknown.isEmpty()) {
                return unknown;
            }
            Set<String> declared = declaredBy(appClass);
            for (String key : unknown) {
                String suggestion = closestDeclared(key, declared);
                final String detail = suggestion == null ? "" : " — did you mean '" + suggestion + "'?";
                logger.logError(() -> "TQL property '" + key + "' is not declared by this adapter"
                        + detail + ". Striim does not reject unknown properties, so nothing reads "
                        + "it and the adapter is running on the declared default. Check it against "
                        + "the version you upgraded from.");
            }
        } catch (Exception | LinkageError e) {
            if (logger != null) {
                logger.log(() -> "could not check declared properties: " + e);
            }
            return new ArrayList<>();
        }
        if (strict(received)) {
            throw new IllegalStateException("TQL propert" + (unknown.size() == 1 ? "y " : "ies ")
                    + unknown + " not declared by this adapter, and " + STRICT_PROPERTY
                    + " is on: refusing to start on a default the TQL did not ask for. Fix the"
                    + " name(s) — the log lines above suggest the intended ones — or set "
                    + STRICT_PROPERTY + ": 'false'.");
        }
        return unknown;
    }

    /** The TQL property when present, else the node-wide system property; both default to off. */
    static boolean strict(Map<String, Object> received) {
        String tql = Props.str(received, STRICT_PROPERTY, null);
        if (tql != null) {
            return isTrue(tql);
        }
        return isTrue(System.getProperty(STRICT_SYSTEM_PROPERTY));
    }

    private static boolean isTrue(String value) {
        if (value == null) {
            return false;
        }
        String v = value.trim().toLowerCase(java.util.Locale.ROOT);
        return v.equals("true") || v.equals("yes") || v.equals("1");
    }

    /**
     * The declared name closest to {@code key}, or {@code null} when none is close.
     *
     * <p>A bare "unknown property X" is much less useful than "did you mean Y?" during a version
     * bump, which is the migration this guard exists for. Case-insensitive edit distance, capped
     * so an unrelated name is not offered as a suggestion.</p>
     */
    public static String closestDeclared(String key, Collection<String> declared) {
        if (key == null || declared == null) {
            return null;
        }
        String best = null;
        int bestDistance = Integer.MAX_VALUE;
        int cap = Math.max(2, key.length() / 3);
        for (String candidate : declared) {
            int d = distance(key.toLowerCase(Locale.ROOT), candidate.toLowerCase(Locale.ROOT));
            if (d < bestDistance) {
                bestDistance = d;
                best = candidate;
            }
        }
        return bestDistance <= cap ? best : null;
    }

    /** Iterative Levenshtein over two rows; no allocation per character. */
    private static int distance(String a, String b) {
        int[] previous = new int[b.length() + 1];
        int[] current = new int[b.length() + 1];
        for (int j = 0; j <= b.length(); j++) {
            previous[j] = j;
        }
        for (int i = 1; i <= a.length(); i++) {
            current[0] = i;
            for (int j = 1; j <= b.length(); j++) {
                int cost = a.charAt(i - 1) == b.charAt(j - 1) ? 0 : 1;
                current[j] = Math.min(Math.min(current[j - 1] + 1, previous[j] + 1),
                                      previous[j - 1] + cost);
            }
            int[] swap = previous;
            previous = current;
            current = swap;
        }
        return previous[b.length()];
    }
}
