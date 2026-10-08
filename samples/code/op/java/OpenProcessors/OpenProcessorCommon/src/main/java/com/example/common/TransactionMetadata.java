package com.example.common;

import java.util.Map;

/**
 * Reads transaction boundaries and a transaction id out of {@code WAEvent} metadata.
 *
 * <p>Key names are not guesses. They follow {@code MetaKeyProvider}, the platform's own per-adapter registry of the
 * metadata each reader publishes. Every CDC reader in that registry — Oracle, SQL Server, DatabaseReader,
 * PostgreSQL, MySQL, MariaDB Xpand — names the transaction id {@value #TXN_ID}. Spanner arrives
 * through a field-built reader that is not in the registry and names it
 * {@value #SERVER_TRANSACTION_ID}.</p>
 *
 * <h2>Why the id is coerced and never cast</h2>
 *
 * <p>The registry declares {@value #TXN_ID} as {@code String} for every reader <b>except
 * PostgreSQL, which declares it {@code Integer}</b>. So {@code (String) metadata.get("TxnID")}
 * compiles, passes against Oracle, SQL Server, DatabaseReader and MySQL, and throws
 * {@link ClassCastException} in production against PostgreSQL alone. This class calls
 * {@link String#valueOf} instead. The cost is that two readers could in principle produce ids that
 * collide once stringified; the benefit is that no dialect is a landmine.</p>
 *
 * <h2>Sources that have no transaction id</h2>
 *
 * <p>{@code DatabaseReader} and {@code IncrementalBatchReader} publish no {@value #TXN_ID} at all —
 * they are not CDC and there is no transaction to preserve. {@link #transactionIdOf} returns null,
 * and a caller in transaction mode must reject the configuration rather than treat every event as
 * one unbounded transaction.</p>
 *
 * <p>SQL Server is the trap in between: {@code MSSqlReader} populates {@value #TXN_ID} <b>only when
 * {@code FetchTransactionMetadata = 'true'}</b>. With that property off the reader is otherwise
 * healthy and every event simply carries a null id, which silently degrades transaction mode into
 * "one transaction, forever" — the guarantee's name kept, its content gone. A caller must treat an
 * all-null id stream as a misconfiguration, which is what {@link #hasTransactionId} exists to let
 * it check.</p>
 *
 * <h2>Boundary markers</h2>
 *
 * <p>BEGIN and COMMIT are not a side channel. They arrive as ordinary events whose
 * {@value #OPERATION_NAME} is the marker, alongside ROLLBACK and the four-valued
 * {@code controltype} enum in {@code com.webaction.source.lib.type}. They are <b>not filtered by
 * default</b>: {@code TxnCacheLayer.filterTxnBoundary} initialises to {@code false}.</p>
 *
 * <p>That matters to a writer for a reason beyond grouping — a marker carries no columns, so a
 * writer that treats it as a row builds a statement from nothing. {@link #isDataEvent} is the
 * guard, and it is deliberately written as "not a marker" rather than an allow-list of DML names,
 * so an operation this code has never seen is still written rather than silently dropped.</p>
 *
 * <p>Markers are an <i>optional</i> signal. Whether they reach a target depends on the reader and
 * its configuration, so {@link TransactionGrouper} groups on id runs and does not require them.
 * When they are present they are the stronger signal, because they mark the boundary explicitly
 * rather than inferring it from a change in id.</p>
 */
public final class TransactionMetadata {

    /** Transaction id key, common to every CDC reader in the platform registry. */
    public static final String TXN_ID = "TxnID";

    /** Transaction id key used by the Spanner change-stream reader, which predates the registry. */
    public static final String SERVER_TRANSACTION_ID = "server_transaction_id";

    /** Operation key; carries a {@link Boundary} name on a marker event and a DML name otherwise. */
    public static final String OPERATION_NAME = "OperationName";

    private TransactionMetadata() {
    }

    /**
     * A transaction boundary marker, or {@link #NONE} for an ordinary data event.
     *
     * <p>Mirrors {@code com.webaction.source.lib.type.controltype}, minus {@code WA_TRUNCATE},
     * which is a DDL event rather than a boundary and is left to flow through as data.</p>
     */
    public enum Boundary {
        /** Start of a source transaction. */
        BEGIN,
        /** Successful end of a source transaction. */
        COMMIT,
        /** Abandoned source transaction; anything grouped under this id must not be applied. */
        ROLLBACK,
        /** Not a marker — an ordinary data event. */
        NONE
    }

    /**
     * Classifies an event by its {@value #OPERATION_NAME}.
     *
     * <p>Matched case-insensitively. The platform writes these upper case, but a marker missed
     * because of case would be read as a data row with no columns, and failing that way for so
     * small a reason is not worth the strictness.</p>
     */
    public static Boundary boundaryOf(final Map<String, Object> metadata) {
        if (metadata == null) {
            return Boundary.NONE;
        }
        final Object op = metadata.get(OPERATION_NAME);
        if (op == null) {
            return Boundary.NONE;
        }
        final String name = String.valueOf(op).trim();
        if ("BEGIN".equalsIgnoreCase(name)) {
            return Boundary.BEGIN;
        }
        if ("COMMIT".equalsIgnoreCase(name)) {
            return Boundary.COMMIT;
        }
        if ("ROLLBACK".equalsIgnoreCase(name)) {
            return Boundary.ROLLBACK;
        }
        return Boundary.NONE;
    }

    /**
     * True when the event carries row data, i.e. when it is not a boundary marker.
     *
     * <p>Defined as the negation of {@link #boundaryOf} rather than as a list of known DML names,
     * so an unrecognised operation is written rather than dropped.</p>
     */
    public static boolean isDataEvent(final Map<String, Object> metadata) {
        return boundaryOf(metadata) == Boundary.NONE;
    }

    /**
     * The transaction id, or null when the source publishes none.
     *
     * <p>Prefers {@value #TXN_ID} and falls back to {@value #SERVER_TRANSACTION_ID}. Coerced with
     * {@link String#valueOf}, never cast — see the class javadoc for why PostgreSQL makes that
     * mandatory. A value present but blank is reported as null, since an id that cannot
     * distinguish one transaction from another is worth no more than an absent one.</p>
     */
    public static String transactionIdOf(final Map<String, Object> metadata) {
        if (metadata == null) {
            return null;
        }
        final String primary = coerce(metadata.get(TXN_ID));
        if (primary != null) {
            return primary;
        }
        return coerce(metadata.get(SERVER_TRANSACTION_ID));
    }

    /**
     * True when this event carries a usable transaction id.
     *
     * <p>Intended for the configuration check a caller in transaction mode owes itself: a stream in
     * which this is never true is a source that cannot support the mode, not a stream that happens
     * to hold one very large transaction.</p>
     */
    public static boolean hasTransactionId(final Map<String, Object> metadata) {
        return transactionIdOf(metadata) != null;
    }

    private static String coerce(final Object value) {
        if (value == null) {
            return null;
        }
        final String text = String.valueOf(value).trim();
        return text.isEmpty() ? null : text;
    }
}
