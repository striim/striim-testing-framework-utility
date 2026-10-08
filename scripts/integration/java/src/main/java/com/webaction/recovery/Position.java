package com.webaction.recovery;

import java.io.Serializable;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Mock of the platform's {@code com.webaction.recovery.Position} — a set of {@link Path}s, and
 * the type a Target's {@code getEndpointCheckpoint()} returns and its checkpoint row holds.
 *
 * <h2>Keyed by identity, not by insertion</h2>
 * The real class keys its map by a path hash. This mock keys by {@link Path#key()}'s hash, which
 * makes {@link #equals} order-independent — load-bearing for the tier's whole point: a case writes
 * a position, restarts the writer, reads it back out of the checkpoint table, and compares. An
 * insertion-ordered key would make that comparison pass or fail on map iteration order rather than
 * on the writer's behaviour.
 *
 * <p>{@code Serializable} because the {@code KryoSingleton} mock round-trips a position through
 * plain Java serialization; see that class for what byte-level fidelity this tier does NOT claim.</p>
 */
public class Position implements Serializable {

    private static final long serialVersionUID = 1L;

    private final Map<Integer, Path> paths = new LinkedHashMap<>();

    /** An empty position — no path, nothing accounted for. */
    public Position() {
    }

    /** A position describing exactly one path. */
    public Position(Path path) {
        add(path);
    }

    /** A position over {@code paths}, keyed by each one's identity. */
    public Position(Collection<Path> paths) {
        for (Path path : paths) {
            add(path);
        }
    }

    private void add(Path path) {
        if (path != null) {
            paths.put(path.key().hashCode(), path);
        }
    }

    /** The identity hashes this position holds paths under. */
    public Set<Integer> keySet() {
        return paths.keySet();
    }

    /** The path held under {@code key}, or null. */
    public Path get(Integer key) {
        return paths.get(key);
    }

    /** True when a path is held under {@code key}. */
    public boolean containsKey(int key) {
        return paths.containsKey(key);
    }

    /** How many paths this position holds. */
    public int size() {
        return paths.size();
    }

    /** True when this position accounts for nothing. */
    public boolean isEmpty() {
        return paths.isEmpty();
    }

    /** Every path, in insertion order. {@code AbstractWriterApp} renders these for monitoring. */
    public Collection<Path> values() {
        return paths.values();
    }

    /** Every path, as a list. */
    public List<Path> toPaths() {
        return new java.util.ArrayList<>(paths.values());
    }

    @Override
    public boolean equals(Object o) {
        return o instanceof Position && paths.equals(((Position) o).paths);
    }

    @Override
    public int hashCode() {
        return paths.hashCode();
    }

    @Override
    public String toString() {
        return "Position" + paths.values();
    }
}
