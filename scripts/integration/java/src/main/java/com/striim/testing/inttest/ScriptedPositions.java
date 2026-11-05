package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.Path;
import com.webaction.recovery.Position;
import com.webaction.uuid.UUID;

/**
 * The recovery position of each input event of a {@code target:} case, and the platform's
 * at-or-equal resume filter over them.
 *
 * <p>An event's {@code "position"} is an ordinal ({@code 3}) or {@code {"ordinal": 3, "path": "b"}};
 * several events may share one, as an upstream fan-out's siblings do. An event without it is at
 * {@code index + 1} on the default path, so a fixture with none is today's 1..n.</p>
 */
final class ScriptedPositions {

    static final String KEY = "position";

    /**
     * The default path's component. Fixed so a case can assert the durable position; {@code -1}
     * keeps it clear of {@link LoadedOp}'s {@code types:} UUIDs.
     */
    static final UUID DEFAULT_COMPONENT = new UUID(0L, -1L);

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final long[] ordinals;
    private final String[] paths;   // null = default path
    private final boolean explicit;

    private ScriptedPositions(long[] ordinals, String[] paths, boolean explicit) {
        this.ordinals = ordinals;
        this.paths = paths;
        this.explicit = explicit;
    }

    /** Today's positions: event i at ordinal i + 1, one path. */
    static ScriptedPositions defaults(int count) {
        long[] ordinals = new long[count];
        for (int i = 0; i < count; i++) {
            ordinals[i] = i + 1L;
        }
        return new ScriptedPositions(ordinals, new String[count], false);
    }

    /** Reads each entry's {@code "position"} from a fixture array. */
    static ScriptedPositions read(String json) throws Exception {
        JsonNode root = MAPPER.readTree(json);
        if (root == null || !root.isArray()) {
            throw new IllegalArgumentException("input JSON fixture must be a top-level array; got: " + root);
        }
        int count = root.size();
        long[] ordinals = new long[count];
        String[] paths = new String[count];
        boolean explicit = false;
        Map<String, Long> last = new HashMap<>();
        for (int i = 0; i < count; i++) {
            JsonNode node = root.get(i).get(KEY);
            ordinals[i] = i + 1L;
            if (node != null && !node.isNull()) {
                explicit = true;
                parse(node, i, ordinals, paths);
            }
            String path = paths[i] == null ? "" : paths[i];
            Long previous = last.put(path, ordinals[i]);
            if (previous != null && ordinals[i] < previous) {
                throw new IllegalArgumentException("Invalid input fixture at array index " + i
                        + ": 'position' " + ordinals[i] + " is below the " + previous
                        + " before it on " + describe(paths[i]) + ". A source emits a path's"
                        + " positions in order; an event without 'position' is at index + 1.");
            }
        }
        return new ScriptedPositions(ordinals, paths, explicit);
    }

    private static void parse(JsonNode node, int i, long[] ordinals, String[] paths) {
        JsonNode ordinal = node;
        if (node.isObject()) {
            for (Iterator<String> it = node.fieldNames(); it.hasNext();) {
                String name = it.next();
                if (!"ordinal".equals(name) && !"path".equals(name)) {
                    throw invalid(i, "'position' has unknown key '" + name
                            + "' (allowed: ordinal, path)");
                }
            }
            ordinal = node.get("ordinal");
            JsonNode path = node.get("path");
            if (path != null && !path.isNull()) {
                if (!path.isTextual() || path.asText().isEmpty()) {
                    throw invalid(i, "'position.path' must be a non-empty string, got " + path);
                }
                paths[i] = path.asText();
            }
        }
        if (ordinal == null || !ordinal.isIntegralNumber() || !ordinal.canConvertToLong()
                || ordinal.asLong() < 1) {
            throw invalid(i, "'position' must be an integer ordinal of at least 1, or"
                    + " {\"ordinal\": N, \"path\": \"name\"}; got " + node);
        }
        ordinals[i] = ordinal.asLong();
    }

    private static IllegalArgumentException invalid(int i, String message) {
        return new IllegalArgumentException("Invalid input fixture at array index " + i + ": " + message);
    }

    private static String describe(String path) {
        return path == null ? "the default path" : "path '" + path + "'";
    }

    /** True when any event named its position. */
    boolean explicit() {
        return explicit;
    }

    int size() {
        return ordinals.length;
    }

    long ordinal(int index) {
        return ordinals[index];
    }

    /** The stemma event {@code index} is handed with. */
    ImmutableStemma stemma(int index, String distributionId) {
        return new ImmutableStemma(component(paths[index]), distributionId,
                new OrdinalPosition(ordinals[index]));
    }

    private static UUID component(String path) {
        return path == null ? DEFAULT_COMPONENT : WAEventJsonFactory.sourceUuidFor("position:" + path);
    }

    private boolean namedPaths() {
        for (String path : paths) {
            if (path != null) {
                return true;
            }
        }
        return false;
    }

    /**
     * The durable ordinal per path component, from the writer's endpoint checkpoint.
     *
     * <p>Only {@link OrdinalPosition} lows count, as before. With no named path every durable path
     * folds into the default one at its highest ordinal, which is the old index resume exactly.</p>
     */
    Map<UUID, Long> durable(Position checkpoint) {
        Map<UUID, Long> out = new LinkedHashMap<>();
        if (checkpoint == null) {
            return out;
        }
        boolean named = namedPaths();
        for (Path path : checkpoint.values()) {
            if (path.getLowSourcePosition() instanceof OrdinalPosition low) {
                UUID key = named ? path.getComponentID() : DEFAULT_COMPONENT;
                out.merge(key, low.ordinal(), Math::max);
            }
        }
        return out;
    }

    /**
     * The platform's at-or-equal filter: an event at or below its path's durable position is
     * dropped on replay. {@code isAfter()} is always false in the stand-in, so equal is dropped.
     */
    boolean filtered(int index, Map<UUID, Long> durable) {
        Long at = durable.get(component(paths[index]));
        return at != null && ordinals[index] <= at;
    }

    /**
     * Refuses a durable position the stream never reached on its path: obeying it would skip
     * events, the data loss an over-advanced checkpoint causes in production.
     *
     * @param fed how many events (from index 0) have been delivered at least once
     */
    void requireReached(Map<UUID, Long> durable, int fed) {
        Map<UUID, Long> highest = new HashMap<>();
        for (int i = 0; i < fed; i++) {
            highest.merge(component(paths[i]), ordinals[i], Math::max);
        }
        for (Map.Entry<UUID, Long> entry : durable.entrySet()) {
            Long reached = highest.get(entry.getKey());
            if (reached == null || entry.getValue() > reached) {
                throw new IllegalStateException("after restarting, the target reports position "
                        + entry.getValue() + " as durable on component " + entry.getKey()
                        + ", but the highest position fed there is "
                        + (reached == null ? "none" : reached) + " (" + fed + " event(s) fed)."
                        + " A position ahead of the stream makes the source resume past data"
                        + " it never delivered.");
            }
        }
    }

    /** For tests: the component each event's path maps to. */
    List<UUID> components() {
        List<UUID> out = new ArrayList<>();
        for (String path : paths) {
            out.add(component(path));
        }
        return out;
    }
}
