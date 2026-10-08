package com.example.common;

import java.lang.reflect.Field;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.UnaryOperator;

import com.webaction.proc.events.WAEvent;

/**
 * {@code WAEvent} copying that does not silently lose metadata.
 *
 * <p>
 * {@code WAEvent.makeCopy} is NOT a full copy: it carries only {@code data}/{@code before}
 * plus their presence bitmaps, {@code metadata}/{@code userdata}/{@code aiData},
 * {@code timeStamp}, {@code typeUUID} and {@code sourceUUID}, dropping every other
 * {@code SimpleEvent} field ({@link #DROPPED_BY_MAKECOPY}) without throwing or logging.
 * The costly one is {@code key}: {@code SimpleEvent implements Partitionable}, so a raw
 * copy loses its partition assignment.
 *
 * <p>
 * Shared by OPs and UDFs alike, so it lives here rather than in
 * {@code OpenProcessorCommon}; consumers add-source it and shade-relocate it into their
 * own jar. The copied fields are listed below.
 */
public final class WAEvents {

    /**
     * The seven {@code SimpleEvent} fields {@code makeCopy} does not carry. Re-stamped
     * reflectively because they are declared on the superclass and there is no platform API
     * to copy them.
     */
    private static final String[] DROPPED_BY_MAKECOPY = {
            "originTimeStamp", "key", "leeEntry", "meteringInfo", "linkedSourceEvents",
            "_wa_SimpleEvent_ID", "_id" };

    /**
     * Field name -> resolved handle, memoized INCLUDING misses (as {@code Optional.empty()}).
     * The miss caching is load-bearing: {@code computeIfAbsent} never caches a null result, so
     * a WAEvent stand-in that doesn't declare a field (the integration-test mock) would re-walk
     * the whole hierarchy throwing {@code NoSuchFieldException} per field per event — measured
     * at >10x throughput regression on the perf suite.
     */
    private static final Map<String, Optional<Field>> FIELD_CACHE = new ConcurrentHashMap<>();

    /**
     * Swappable purely so tests can install a counting decorator and assert how many copies a
     * call path actually made.
     */
    static UnaryOperator<WAEvent> copier = WAEvents::defaultCopyEvent;

    private WAEvents() {
    }

    /**
     * Deep-copies {@code event} via {@code WAEvent.makeCopy}, then re-stamps the seven fields
     * it drops. Use this instead of calling {@code WAEvent.makeCopy} directly. {@code null} in,
     * {@code null} out.
     */
    public static WAEvent copyEvent(final WAEvent event) {
        return event == null ? null : copier.apply(event);
    }

    /**
     * Re-stamps the seven dropped fields from {@code src} onto {@code dst}. For callers that
     * construct a {@code WAEvent} themselves rather than going through {@link #copyEvent} —
     * a hand-built event drops the same fields for the same reason. No-op if either is null.
     */
    public static void restampDroppedFields(final WAEvent src, final WAEvent dst) {
        if (src == null || dst == null) {
            return;
        }
        for (final String name : DROPPED_BY_MAKECOPY) {
            copyField(src, dst, name);
        }
    }

    private static WAEvent defaultCopyEvent(final WAEvent event) {
        final WAEvent copy = WAEvent.makeCopy(event);
        restampDroppedFields(event, copy);
        return copy;
    }

    /**
     * Object-typed fields (leeEntry/meteringInfo/linkedSourceEvents) copy by reference, same as
     * makeCopy's own data[]/before[] element handling.
     */
    private static void copyField(final WAEvent src, final WAEvent dst, final String name) {
        try {
            final Field field = FIELD_CACHE
                    .computeIfAbsent(name, n -> Optional.ofNullable(findField(src.getClass(), n)))
                    .orElse(null);
            if (field != null) {
                field.set(dst, field.get(src));
            }
        } catch (final Throwable ignored) {
            // best-effort: this WAEvent stand-in doesn't declare the field, or it isn't accessible
        }
    }

    /**
     * Any resolution failure must return null rather than escape, so the caller's
     * {@code computeIfAbsent} still memoizes the miss — an escaped exception would leave it
     * uncached and reintroduce the per-event throw/catch cost the cache exists to avoid.
     */
    private static Field findField(final Class<?> startClass, final String name) {
        try {
            for (Class<?> cls = startClass; cls != null; cls = cls.getSuperclass()) {
                try {
                    final Field field = cls.getDeclaredField(name);
                    field.setAccessible(true);
                    return field;
                } catch (final NoSuchFieldException ignored) {
                    // keep walking up to the next superclass
                }
            }
            return null;
        } catch (final Throwable ignored) {
            return null;
        }
    }
}
