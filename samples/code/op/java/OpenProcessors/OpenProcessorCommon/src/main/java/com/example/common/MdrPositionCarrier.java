package com.example.common;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.MapperFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.recovery.InitialImmutableStemma;
import com.webaction.recovery.JSONSourcePosition;
import com.webaction.recovery.SourcePosition;
import com.webaction.recovery.Stemma;
import com.webaction.uuid.UUID;

import java.io.Serializable;
import java.util.Collections;
import java.util.Set;

/**
 * System-classpath carrier bridge for persisting and recovering Open Processor state in Striim's
 * Metadata Repository (MDR) without creating local disk files.
 *
 * <p><b>Why a Carrier is Required (The Classloader Trap):</b> Striim persists checkpoints to the MDR
 * database ({@code StriimCheckpoints} table) by serializing {@link ComponentCheckpoint} to JSON using
 * Jackson. Because {@link SourcePosition} is annotated with {@code @JsonTypeInfo(Id.CLASS)}, Jackson
 * embeds the fully-qualified class name of the position object. On platform restart, {@code StatusDataStore}
 * and {@code Flow} deserialize checkpoints under {@code StriimClassLoader} (the system classloader).
 * Because plugin classes are loaded in isolated {@code ModuleClassLoader} child loaders, Jackson
 * fails to resolve plugin-classloader position classes, throwing {@code ClassNotFoundException} and
 * resetting recovery.</p>
 *
 * <p><b>The Solution:</b> This carrier serializes the plugin's typed position to clean JSON and places
 * it inside {@link JSONSourcePosition} (which lives in {@code Common.jar} on the system classpath)
 * and inside {@link ComponentCheckpoint#getComponentState()}. On startup, the platform deserializes
 * {@link JSONSourcePosition} with zero errors, and hands it to {@code init()}, where this helper
 * deserializes it back into the plugin's typed position.</p>
 *
 * <p>Top-level named class per the platform classloader contract.</p>
 */
public final class MdrPositionCarrier {

    private static final Logger logger = new Logger("MdrPositionCarrier", false);

    private static final ObjectMapper MAPPER = new ObjectMapper()
            .disable(MapperFeature.USE_ANNOTATIONS)
            .disable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private MdrPositionCarrier() {
    }

    /**
     * Serializes any position or state object to JSON using standard settings.
     */
    public static String serialize(Object position) throws Exception {
        if (position == null) {
            return null;
        }
        return MAPPER.writeValueAsString(position);
    }

    /**
     * Wraps a typed position into a platform {@link JSONSourcePosition} carrier.
     *
     * @param position typed position to serialize
     * @param sequenceNo sequence number or watermark timestamp for ordering
     * @return a system-classpath {@link JSONSourcePosition} carrier
     * @throws Exception if JSON serialization fails
     */
    public static <P> JSONSourcePosition toCarrier(P position, long sequenceNo) throws Exception {
        if (position == null) {
            return null;
        }
        String json = serialize(position);
        return new JSONSourcePosition(json, sequenceNo);
    }

    /**
     * Constructs a native {@link ComponentCheckpoint} carrying the serialized position both in
     * an {@link InitialImmutableStemma} (via {@link JSONSourcePosition}) and in {@code componentState}.
     *
     * @param position typed position to persist
     * @param sourceUUID the source component's UUID
     * @param distributionID partition distribution ID, or null if unpartitioned
     * @param sequenceNo watermark timestamp or monotonic sequence number
     * @return fully constructed {@link ComponentCheckpoint} ready for MDR persistence
     * @throws Exception if serialization fails
     */
    public static <P> ComponentCheckpoint toComponentCheckpoint(
            P position,
            UUID sourceUUID,
            String distributionID,
            long sequenceNo) throws Exception {
        if (position == null || sourceUUID == null) {
            return null;
        }
        String json = serialize(position);
        JSONSourcePosition carrier = new JSONSourcePosition(json, sequenceNo);
        InitialImmutableStemma stemma = ImmutableStemma.from(
                sourceUUID, distributionID, carrier, carrier, Stemma.AT);
        Set<ImmutableStemma> stemmas = Collections.singleton(stemma);
        return new ComponentCheckpoint(System.currentTimeMillis(), sourceUUID, stemmas, json);
    }

    /**
     * Extracts a typed position from a variety of recovery sources:
     * <ul>
     *   <li>An existing instance of {@code positionClass} (unit tests / in-memory handoff)</li>
     *   <li>A {@link JSONSourcePosition} (platform restart handoff to {@code init()})</li>
     *   <li>A {@link ComponentCheckpoint} (direct MDR restore via {@code componentState} or stemma)</li>
     *   <li>A raw JSON string</li>
     * </ul>
     *
     * @param candidate object received from the framework or store
     * @param positionClass target class to deserialize
     * @param <P> target position type
     * @return deserialized position instance, or null if candidate is null/unparseable
     */
    public static <P> P extractPosition(Object candidate, Class<P> positionClass) {
        if (candidate == null || positionClass == null) {
            return null;
        }
        if (positionClass.isInstance(candidate)) {
            return positionClass.cast(candidate);
        }
        try {
            if (candidate instanceof JSONSourcePosition) {
                JSONSourcePosition jsp = (JSONSourcePosition) candidate;
                String json = jsp.getSourceName();
                if (json == null || json.trim().isEmpty()) {
                    return null;
                }
                return MAPPER.readValue(json, positionClass);
            }
            if (candidate instanceof ComponentCheckpoint) {
                ComponentCheckpoint cc = (ComponentCheckpoint) candidate;
                // 1. Try componentState first (fast, direct JSON)
                Serializable rawState = cc.getComponentState();
                if (rawState != null) {
                    String state = rawState.toString().trim();
                    if (!state.isEmpty()) {
                        try {
                            return MAPPER.readValue(state, positionClass);
                        } catch (Exception e) {
                            logger.log(() -> "ComponentCheckpoint componentState could not be parsed as "
                                    + positionClass.getSimpleName() + ": " + e.getMessage());
                        }
                    }
                }
                // 2. Try stemma positions
                UUID compUuid = cc.getComponentUuid();
                SourcePosition sp = null;
                if (compUuid != null) {
                    try {
                        sp = cc.getLowestSourcePosition(compUuid);
                    } catch (Exception ignored) {
                    }
                }
                if (sp instanceof JSONSourcePosition) {
                    return extractPosition(sp, positionClass);
                }
            }
            if (candidate instanceof String) {
                String str = ((String) candidate).trim();
                if (!str.isEmpty() && (str.startsWith("{") || str.startsWith("["))) {
                    return MAPPER.readValue(str, positionClass);
                }
            }
        } catch (Exception e) {
            logger.logWarn(() -> "Failed to extract " + positionClass.getSimpleName()
                    + " from candidate of type " + candidate.getClass().getName() + ": " + e.getMessage());
        }
        return null;
    }

    /**
     * Builds a {@link ComponentCheckpoint} carrying state for non-source Open Processors
     * (transformers, writers, sinks) using {@link ComponentCheckpoint#getComponentState()}.
     *
     * @param state state POJO or String
     * @param componentUuid component UUID
     * @return {@link ComponentCheckpoint} ready for MDR persistence via StatusDataStore
     * @throws Exception if serialization fails
     */
    public static ComponentCheckpoint toStateCheckpoint(Object state, UUID componentUuid) throws Exception {
        if (state == null || componentUuid == null) {
            return null;
        }
        String json = (state instanceof String) ? (String) state : MAPPER.writeValueAsString(state);
        return new ComponentCheckpoint(System.currentTimeMillis(), componentUuid, Collections.emptySet(), json);
    }

    /**
     * Extracts state from a {@link ComponentCheckpoint} for non-source Open Processors.
     */
    public static <S> S extractState(ComponentCheckpoint checkpoint, Class<S> stateClass) {
        if (checkpoint == null || stateClass == null) {
            return null;
        }
        Serializable rawState = checkpoint.getComponentState();
        if (rawState == null) {
            return null;
        }
        String json = rawState.toString().trim();
        if (json.isEmpty()) {
            return null;
        }
        if (stateClass == String.class) {
            return stateClass.cast(json);
        }
        try {
            return MAPPER.readValue(json, stateClass);
        } catch (Exception e) {
            logger.logWarn(() -> "Failed to extract state " + stateClass.getSimpleName()
                    + " from ComponentCheckpoint: " + e.getMessage());
            return null;
        }
    }
}
