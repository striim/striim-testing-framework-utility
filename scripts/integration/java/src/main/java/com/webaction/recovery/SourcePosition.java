package com.webaction.recovery;

import java.io.Serializable;

/**
 * Mock of the platform's {@code com.webaction.recovery.SourcePosition}.
 *
 * <p><b>Why a reader in the {@code SourcePosition} family needs this.</b> Loading
 * a change-data or API-polling reader's {@code Processor} resolves its constructor descriptor,
 * which names the module's own position class — and that class {@code extends SourcePosition}. Java
 * cannot load the subclass without the supertype, so without this file the core cannot be
 * constructed at all and the tier is impossible. Exactly the reason
 * {@link ComponentCheckpoint} is mocked for the other recovery family.</p>
 *
 * <p><b>The first reader with a tier hid this gap.</b> It was the reader-tier pathfinder, and it belongs to
 * the {@code ComponentCheckpoint} family whose mock already existed — so the missing half stayed
 * invisible until the first {@code SourcePosition}-family reader tried to build a tier.
 * The pathfinder was not representative, the same way it was not for the channel seam.</p>
 *
 * <p><b>Shape matches the platform class's public surface on 5.4</b>:
 * abstract, {@code Serializable}, {@code Comparable<SourcePosition>}, one abstract
 * {@code compareTo}, and two {@code toString}-delegating helpers a core may call. The real class
 * also carries a Jackson {@code @JsonTypeInfo(CLASS)} annotation; it is deliberately NOT reproduced
 * — a reader that persists its position may explicitly DISABLES that annotation when
 * serialising its position (it would write a plugin-classloader class name into every checkpoint
 * file), so mirroring it here would misrepresent what the op actually persists.</p>
 *
 * <p>The harness never compares or serialises positions: it has no recovery framework and no
 * restart. What it needs is for the type to exist and to be extensible.</p>
 */
public abstract class SourcePosition implements Serializable, Comparable<SourcePosition> {

    private static final long serialVersionUID = 1657905561025367794L;

    @Override
    public abstract int compareTo(SourcePosition other);

    public String toHumanReadableString() {
        return this.toString();
    }

    public String toTypeNameCompatibleString() {
        return this.toString();
    }
}
