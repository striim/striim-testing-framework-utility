package com.webaction.runtime;

/**
 * Mock of the platform's {@code com.webaction.runtime.ActionType}: the action an
 * {@code ExceptionEvent} records. {@code ExceptionStorePublisher} files a skipped row under
 * {@link #IGNORE}, as {@code NotifyExceptionStore}'s default action is. Every constant of the
 * real enum on 5.4.0.6G, in its order; its lookup helpers are omitted, as nothing in this tier
 * calls them.
 */
public enum ActionType {
    CREATE,
    START,
    VERIFY_START,
    START_SOURCES,
    STOP,
    QUIESCE,
    IL_QUIESCE,
    PAUSE_SOURCES,
    APPROVE_QUIESCE,
    UNPAUSE_SOURCES,
    CHECKPOINT,
    QUIESCE_FLUSH,
    QUIESCE_CHECKPOINT,
    CRASH,
    TERMINATE,
    HALT,
    DEPLOY,
    INIT_CACHES,
    START_CACHES,
    STOP_CACHES,
    UNDEPLOY,
    RESUME,
    STATUS,
    MEMORY_STATUS,
    START_ADHOC,
    STOP_ADHOC,
    CONTINUE,
    IGNORE,
    RETRY,
    SHUTDOWN,
    LOAD,
    UNLOAD,
    BOOT,
    RESTART,
    CANCEL_OP,
    START_DATAVALIDATION,
    STOP_DATAVALIDATION
}
