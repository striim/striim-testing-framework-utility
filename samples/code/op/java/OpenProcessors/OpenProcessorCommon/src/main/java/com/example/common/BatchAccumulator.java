package com.example.common;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Accumulates items until a size threshold or an age deadline is reached.
 *
 * <p>Platform-free on purpose. A Striim writer receives events ONE AT A TIME —
 * {@code RetriableWriter.handleEvent} calls {@code processEvent(channel, event, pos)} per event —
 * so batching is the writer's own job, and it is the part worth unit-testing. Keeping it here,
 * free of {@code Event} and {@code Connection}, is what lets it be tested at all: the platform
 * shell that owns it cannot be constructed outside a running server.</p>
 *
 * <p>Not thread-safe. The writer's {@code receive} path is already serialised on the adapter's
 * {@code synchObject}, so adding a second lock here would be misleading rather than safer.</p>
 */
public final class BatchAccumulator<T> {

    private final int maxItems;
    private final long maxAgeMillis;
    private List<T> items;
    private long firstAddedAt = -1L;
    private long lastAddedAt = -1L;

    /**
     * @param maxItems     flush at or above this count; must be positive
     * @param maxAgeMillis flush when the oldest item is at least this old; 0 disables the deadline
     */
    public BatchAccumulator(final int maxItems, final long maxAgeMillis) {
        if (maxItems <= 0) {
            throw new IllegalArgumentException("maxItems must be > 0, got " + maxItems);
        }
        if (maxAgeMillis < 0) {
            throw new IllegalArgumentException("maxAgeMillis must be >= 0, got " + maxAgeMillis);
        }
        this.maxItems = maxItems;
        this.maxAgeMillis = maxAgeMillis;
        this.items = new ArrayList<>(Math.min(maxItems, 1024));
    }

    /** Adds one item. Returns true when the batch is now due to be flushed. */
    public boolean add(final T item, final long nowMillis) {
        if (items.isEmpty()) {
            firstAddedAt = nowMillis;
        }
        lastAddedAt = nowMillis;
        items.add(item);
        return isDue(nowMillis);
    }

    /** Millis since the newest item arrived; {@code Long.MAX_VALUE} when nothing is held. */
    public long idleMillis(final long nowMillis) {
        return items.isEmpty() ? Long.MAX_VALUE : nowMillis - lastAddedAt;
    }

    /**
     * Whether a flush is due. Checked on add and again by the caller's timer, because a batch that
     * stops receiving events before reaching {@code maxItems} would otherwise sit indefinitely —
     * and its positions stay unacked, holding the app checkpoint back the whole time.
     */
    public boolean isDue(final long nowMillis) {
        if (items.isEmpty()) {
            return false;
        }
        if (items.size() >= maxItems) {
            return true;
        }
        return maxAgeMillis > 0 && (nowMillis - firstAddedAt) >= maxAgeMillis;
    }

    /**
     * Removes and returns everything accumulated. The returned list is a snapshot the caller owns;
     * the accumulator keeps no reference to it, so a caller that fails mid-apply can retry with the
     * same list without it being mutated underneath.
     */
    public List<T> drain() {
        if (items.isEmpty()) {
            return Collections.emptyList();
        }
        // Hand over the list and start a fresh one, rather than copying. A copy is O(batch) on
        // every flush -- 5,000 element writes per batch bought nothing, since the contract is
        // already that the accumulator keeps no reference to what it returns.
        final List<T> drained = items;
        items = new ArrayList<>(Math.min(maxItems, 1024));
        firstAddedAt = -1L;
        lastAddedAt = -1L;
        return drained;
    }

    /**
     * Removes and returns the first {@code count} items, keeping the rest in arrival order. The
     * kept tail is a window that has OVERFLOWED on purpose: a caller holding an
     * open source transaction back cuts in front of it, and the tail keeps accumulating until the
     * transaction is known complete. The age clock restarts for the tail, so an overflow does not
     * make the next deadline fire at once.
     *
     * @param count how many to take; {@code >= size()} is the same as {@link #drain()}, and
     *              {@code 0} takes nothing
     */
    public List<T> drain(final int count, final long nowMillis) {
        if (count < 0) {
            throw new IllegalArgumentException("count must be >= 0, got " + count);
        }
        if (count >= items.size()) {
            return drain();
        }
        if (count == 0) {
            return Collections.emptyList();
        }
        final List<T> head = new ArrayList<>(items.subList(0, count));
        final List<T> tail = new ArrayList<>(items.subList(count, items.size()));
        items = tail;
        firstAddedAt = nowMillis;
        return head;
    }

    /**
     * Removes and returns every item NOT in {@code held}, in arrival order, keeping the held ones
     * in their order. The kept items are a window that has OVERFLOWED on purpose:
     * a caller holding open source transactions back names their items, and everything else --
     * whichever source it came from -- is applied now. The age clock restarts for what is kept.
     *
     * @param held indices into {@link #snapshot()} to keep; an empty set is {@link #drain()}
     */
    public List<T> drainExcept(final java.util.BitSet held, final long nowMillis) {
        if (held == null || held.isEmpty()) {
            return drain();
        }
        final List<T> taken = new ArrayList<>(items.size());
        final List<T> kept = new ArrayList<>(Math.min(held.cardinality(), items.size()));
        for (int i = 0; i < items.size(); i++) {
            (held.get(i) ? kept : taken).add(items.get(i));
        }
        items = kept;
        firstAddedAt = kept.isEmpty() ? -1L : nowMillis;
        if (kept.isEmpty()) {
            lastAddedAt = -1L;
        }
        return taken;
    }

    /** A read-only view of what is held, in arrival order, for a caller deciding where to cut. */
    public List<T> snapshot() {
        return Collections.unmodifiableList(items);
    }

    /** Items currently held, not yet drained. */
    public int size() {
        return items.size();
    }

    /** Whether nothing is held; the age trigger cannot fire while true. */
    public boolean isEmpty() {
        return items.isEmpty();
    }

    /** The item count that makes the batch ready. */
    public int maxItems() {
        return maxItems;
    }

    /** How long the oldest held item may wait before the batch is ready regardless of size. */
    public long maxAgeMillis() {
        return maxAgeMillis;
    }
}
