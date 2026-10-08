package com.example.common;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.Iterator;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * Rejects config keys a parser does not read.
 *
 * <p>Every OP parses its JSON config pull-style ({@code node.get("key")}), which silently ignores
 * anything it was not asked for. A misplaced or misspelled key therefore changes what the app does
 * with no error — a filter written one level down inside a nested object disappears, taking
 * the predicate it carried with it. Call {@link #check} at the top of
 * each parse function with that JSON object level's full key set.
 *
 * <p>Default is warn-only: unknown keys are logged, and recorded (see {@link ConfigIssues}), but
 * tolerated — making this fatal unconditionally would stop deployed configs that carry stray keys
 * and start today. A config opts into rejection with {@code "rejectUnknownKeys": true} at its root,
 * which each {@code OrmFactory} reads and passes to {@link #beginParse}.
 *
 * <p>Rejection lives in the config document rather than an operator property so it travels with the
 * file, so the offline verifier sees exactly what the runtime will, and so every OP gets it from
 * this shared layer without each growing a {@code @PropertyTemplate} entry.
 */
public final class ConfigKeys {

    /** Root-level config key by which a document opts into rejection. */
    public static final String REJECT_UNKNOWN_KEYS = "rejectUnknownKeys";

    /** Suggest a known key when it is within this edit distance of the unknown one. */
    private static final int SUGGEST_MAX_DISTANCE = 2;

    private static final Logger LOG = Logger.of("ConfigKeys", false);

    /**
     * A stack, not a single value: nesting would otherwise let an inner document's
     * {@link #endParse} clear the outer one's posture and silently downgrade the rest of the outer
     * parse. No OP nests {@code create} today, but this class is shared by all of them.
     */
    private static final ThreadLocal<Deque<ConfigParseScope>> SCOPES = new ThreadLocal<>();

    private ConfigKeys() {
    }

    /**
     * Opens a parse scope on this thread. Pass the root document's {@link #REJECT_UNKNOWN_KEYS}
     * value. Always pair with {@link #endParse} in a {@code finally} — a leaked scope would apply
     * one config's posture to the next parse on the same thread.
     */
    public static void beginParse(boolean rejectUnknownKeys) {
        beginParse(rejectUnknownKeys, null);
    }

    /**
     * @param source the config file being read, named in the summary line closed by
     *               {@link #endParse}. Pass it whenever it is known.
     */
    public static void beginParse(boolean rejectUnknownKeys, String source) {
        Deque<ConfigParseScope> stack = SCOPES.get();
        if (stack == null) {
            stack = new ArrayDeque<>();
            SCOPES.set(stack);
        }
        stack.push(new ConfigParseScope(rejectUnknownKeys, source));
    }

    /** Closes the scope opened by {@link #beginParse}, restoring any enclosing one. */
    public static void endParse() {
        Deque<ConfigParseScope> stack = SCOPES.get();
        if (stack == null)
            return;
        ConfigParseScope scope = stack.pop();
        if (stack.isEmpty())
            SCOPES.remove();
        // One line per config load, so "does this deployment carry stray keys, and how many" is a
        // single grep rather than a count across per-location lines. Only emitted when something was
        // tolerated, so a clean load stays silent.
        if ((scope.keys > 0 || scope.shapes > 0) && !ConfigIssues.isCollecting()) {
            final String where = (scope.source != null) ? scope.source : "config";
            final int k = scope.keys, sh = scope.shapes, loc = scope.locations;
            // Keys and shapes are different defects; folding them together would report a key
            // count that no line in the log accounts for.
            final String what = (sh == 0) ? k + " unknown config key(s)"
                    : (k == 0) ? sh + " wrong-shaped value(s)"
                    : k + " unknown config key(s) and " + sh + " wrong-shaped value(s)";
            LOG.logWarn(() -> what + " tolerated across " + loc + " location(s) in " + where
                    + " — set \"" + REJECT_UNKNOWN_KEYS + "\": true to reject instead");
        }
    }

    /**
     * Reads the root document's {@link #REJECT_UNKNOWN_KEYS} value for {@link #beginParse}. Absent
     * means tolerate.
     *
     * <p>Rejects a non-boolean rather than coercing it. {@code asBoolean(false)} would read
     * {@code "TRUE"}, {@code "yes"} and {@code "1"} as <em>false</em> — silently leaving the check
     * off for an author who believes they turned it on, which is the exact silent-misconfiguration
     * failure this class exists to eliminate.</p>
     *
     * @throws IllegalArgumentException if the key is present but not a JSON boolean
     */
    public static boolean readPosture(JsonNode root) {
        if (root == null || !root.isObject())
            return false;
        JsonNode node = root.get(REJECT_UNKNOWN_KEYS);
        if (node == null || node.isNull())
            return false;
        if (!node.isBoolean())
            throw new IllegalArgumentException("\"" + REJECT_UNKNOWN_KEYS
                    + "\" must be a JSON boolean (true or false), found " + node
                    + " at (root); a quoted \"true\" would silently mean false");
        return node.booleanValue();
    }

    /**
     * JSON has no comment syntax, so a leading underscore marks an annotation rather than a key the
     * parser should recognise — the convention several checked-in example configs already use
     * ({@code _note}, {@code _scenario}). Never reported.
     */
    private static boolean isComment(String fieldName) {
        return fieldName.startsWith("_");
    }

    /**
     * @param node        the JSON object to check; null or non-object is a no-op, so callers can
     *                    pass an optional child node straight through
     * @param contextPath where this object sits, for the message — e.g.
     *                    {@code lookups[0].layers[1].bootstrap}
     * @param known       every key the parser reads at this level
     */
    public static void check(JsonNode node, String contextPath, Set<String> known) {
        if (node == null || !node.isObject())
            return;

        List<String> unknown = new ArrayList<>();
        Iterator<String> names = node.fieldNames();
        while (names.hasNext()) {
            String name = names.next();
            if (!known.contains(name) && !isComment(name))
                unknown.add(name);
        }
        if (unknown.isEmpty())
            return;

        Collections.sort(unknown);
        report(message(unknown, contextPath, known), unknown.size());
    }

    /**
     * Reports a value that is present but is not an array, at a level where the parser would
     * otherwise skip it in silence.
     *
     * <p>Same failure shape as an unknown key and the same reason to care: writing
     * {@code "filters": { ... }} instead of {@code "filters": [ ... ]} makes the whole value vanish
     * — for a lookup that silently drops its WHERE predicate, which is the
     * defect this class exists to catch, wearing a different hat.</p>
     *
     * <p>Absent or explicit null is not reported: those legitimately mean "not set". Posture is
     * shared with {@link #check} — a warning by default, an error when the document sets
     * {@code rejectUnknownKeys}.</p>
     */
    public static void expectArray(JsonNode node, String contextPath) {
        if (node == null || node.isNull() || node.isArray())
            return;
        reportShape(contextPath + " must be an array, found " + node.getNodeType()
                + " — a value of the wrong shape here is ignored entirely");
    }

    /**
     * Reports a value outside the set the parser can act on, at a level where an unrecognised value
     * would otherwise be silently inert.
     *
     * <p>Posture-gated like {@link #check} and {@link #expectArray}: a warning by default, an error
     * only when the document opts in. That matters here — an unrecognised enum VALUE simply never
     * matches, so it is harmless in a deployed config, and making it fatal unconditionally would
     * stop apps that start today.</p>
     *
     * @param value   the value as written, already normalised for comparison
     * @param allowed every value the parser can act on
     */
    public static void expectOneOf(String value, Set<String> allowed, String contextPath) {
        if (value == null || allowed.contains(value))
            return;
        reportShape(contextPath + " has unrecognised value '" + value + "' — it matches no "
                + "event and is therefore ignored; expected one of " + new TreeSet<>(allowed));
    }

    /**
     * A defect that is not an unknown key — a wrong shape or an unrecognised value. Counted
     * separately so the summary line cannot report a key count that no log line accounts for.
     */
    private static void reportShape(String message) {
        report(message, 0, 1);
    }

    /**
     * Shared by {@link #check}, {@link #expectArray} and {@link #expectOneOf}: log, count,
     * throw-or-tolerate.
     *
     * @param keyCount how many unknown KEYS this finding represents — 0 for a shape or value error,
     *                 which is one location but no key. The summary reports keys, shapes and
     *                 locations separately, so neither can inflate the other.
     */
    private static void report(final String message, int keyCount) {
        report(message, keyCount, 0);
    }

    private static void report(final String message, int keyCount, int shapeCount) {
        Deque<ConfigParseScope> stack = SCOPES.get();
        ConfigParseScope scope = (stack != null && !stack.isEmpty()) ? stack.peek() : null;
        boolean optedIn = scope != null && scope.rejecting;

        // Log on both paths. When rejecting, the throw is what stops the app, but the exception can
        // be wrapped or swallowed on its way up through the platform, and the log line is what
        // actually reaches striim.server.log.
        if (optedIn && !ConfigIssues.isCollecting()) {
            LOG.logError(() -> message);
            throw new IllegalArgumentException(message);
        }
        if (ConfigIssues.isCollecting()) {
            // A verifier is reporting these itself; logging too would print every finding twice and
            // make one look like two.
            ConfigIssues.record(message);
            return;
        }
        if (scope != null) {
            // Counted HERE, past the throw above, and not at the call site. A rejected value is not
            // a tolerated one: incrementing before the throw left the count behind on a scope that
            // had refused the value, and endParse then reported it as "tolerated" while the same
            // run logged it as rejected. That is why all three move together.
            scope.keys += keyCount;
            scope.shapes += shapeCount;
            scope.locations++;
        }
        LOG.logWarn(optedIn
                ? () -> message
                : () -> message + " [tolerated; set \"" + REJECT_UNKNOWN_KEYS
                        + "\": true at the config root to reject]");
    }

    private static String message(List<String> unknown, String contextPath, Set<String> known) {
        StringBuilder sb = new StringBuilder();
        sb.append(unknown.size() == 1 ? "Unknown config key " : "Unknown config keys ");
        for (int i = 0; i < unknown.size(); i++) {
            if (i > 0)
                sb.append(", ");
            sb.append('\'').append(unknown.get(i)).append('\'');
            String suggestion = nearest(unknown.get(i), known);
            if (suggestion != null)
                sb.append(" (did you mean '").append(suggestion).append("'?)");
        }
        sb.append(" at ").append(contextPath);
        sb.append("; known keys here: ").append(new TreeSet<>(known));
        return sb.toString();
    }

    /**
     * Closest known key within {@link #SUGGEST_MAX_DISTANCE} edits, or null. Ties break
     * lexicographically so the message is deterministic.
     */
    private static String nearest(String unknown, Set<String> known) {
        String best = null;
        int bestDistance = Integer.MAX_VALUE;
        for (String candidate : new TreeSet<>(known)) {
            int distance = editDistance(unknown, candidate);
            if (distance < bestDistance) {
                bestDistance = distance;
                best = candidate;
            }
        }
        return (bestDistance <= SUGGEST_MAX_DISTANCE) ? best : null;
    }

    private static int editDistance(String a, String b) {
        int[] previous = new int[b.length() + 1];
        int[] current = new int[b.length() + 1];
        for (int j = 0; j <= b.length(); j++)
            previous[j] = j;
        for (int i = 1; i <= a.length(); i++) {
            current[0] = i;
            for (int j = 1; j <= b.length(); j++) {
                int substitute = previous[j - 1] + (a.charAt(i - 1) == b.charAt(j - 1) ? 0 : 1);
                current[j] = Math.min(substitute, Math.min(previous[j] + 1, current[j - 1] + 1));
            }
            int[] swap = previous;
            previous = current;
            current = swap;
        }
        return previous[b.length()];
    }
}
