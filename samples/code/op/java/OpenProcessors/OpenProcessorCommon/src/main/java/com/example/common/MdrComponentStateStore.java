package com.example.common;

import com.webaction.metaRepository.StatusDataStore;
import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.uuid.UUID;

import java.util.function.Supplier;

/**
 * Production {@link ComponentStateStore} backed by the platform {@code StatusDataStore}.
 * Degrades on any {@code Throwable} — including the {@code LinkageError} raised where
 * {@code StatusDataStore} is not linkable (an agent node) — warning once per direction and
 * never throwing into the owning component.
 */
public final class MdrComponentStateStore implements ComponentStateStore {

    private final Logger logger;
    private final Supplier<StatusDataStore> storeSupplier;
    private volatile boolean getFailedWarned;
    private volatile boolean putFailedWarned;

    /**
     * Link-safe factory: returns null when the platform store cannot even be linked (a JVM
     * without the Striim metadata jars, e.g. the integration harness), so consumers degrade to
     * "no recovery" instead of failing to construct. Callers must tolerate a null result.
     */
    public static MdrComponentStateStore tryCreate(Logger logger) {
        try {
            return new MdrComponentStateStore(logger);
        } catch (Throwable t) {
            if (logger != null) {
                logger.logWarn(() -> "StatusDataStore unavailable in this JVM (" + t
                        + "); MDR component state recovery is disabled");
            }
            return null;
        }
    }

    public MdrComponentStateStore(Logger logger) {
        this(logger, MdrComponentStateStore::currentStore);
    }

    /** Test seam: the supplier supplies the platform store, or throws the failure to degrade. */
    MdrComponentStateStore(Logger logger, Supplier<StatusDataStore> storeSupplier) {
        this.logger = logger;
        this.storeSupplier = storeSupplier;
    }

    private static StatusDataStore currentStore() {
        return StatusDataStore.getInstance();
    }

    @Override
    public ComponentCheckpoint get(UUID componentUuid) {
        if (componentUuid == null) {
            return null;
        }
        try {
            StatusDataStore sds = storeSupplier.get();
            return (sds == null) ? null : sds.getComponentCheckpoint(componentUuid);
        } catch (Throwable t) {
            if (!getFailedWarned && logger != null) {
                getFailedWarned = true;
                logger.logWarn(() -> "MDR component state lookup unavailable: " + t);
            }
        }
        return null;
    }

    @Override
    public boolean put(ComponentCheckpoint checkpoint) {
        if (checkpoint == null) {
            return false;
        }
        try {
            StatusDataStore sds = storeSupplier.get();
            if (sds != null) {
                return sds.persistComponentCheckpoint(checkpoint);
            }
        } catch (Throwable t) {
            if (!putFailedWarned && logger != null) {
                putFailedWarned = true;
                logger.logWarn(() -> "MDR component state persistence unavailable: " + t);
            }
        }
        return false;
    }
}
