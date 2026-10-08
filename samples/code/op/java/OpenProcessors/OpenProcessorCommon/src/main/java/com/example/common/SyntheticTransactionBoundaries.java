package com.example.common;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;

import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;

/**
 * Wraps what an OP emits in a synthetic {@code BEGIN}/{@code COMMIT} pair, so that fan-out
 * siblings — which all carry the source event's single recovery position — reach a writer inside
 * one transaction and commit atomically.
 *
 * <p><b>The defect this closes</b> is: N events at one
 * position split across a commit boundary, the committed part records the shared position in the
 * writer's checkpoint, and the uncommitted remainder is filtered out of the replay as a
 * duplicate. The product's own fix is to let the source's REAL boundaries through (§8.1); this
 * class is for the stream that has none — a reader with {@code FilterTransactionBoundaries: true},
 * a non-CDC reader, a CQ that stripped them.</p>
 *
 * <p><b>Scope</b> ({@value #SCOPE_PROPERTY}) is the unit one pair wraps:</p>
 * <ul>
 *   <li>{@code none} (the default) — no synthetic transactions; the OP forwards what it
 *       forwards.</li>
 *   <li>{@code event} — one source event's fan-out. {@link #apply} is that shape.</li>
 *   <li>{@code batch} — everything one platform batch delivered to {@code run()}: behind a
 *       window, the window's chunk; behind a bare CQ the platform batch is one event, so it is
 *       {@code event} again. The outputs are held ({@link #absorb}) and released together as ONE
 *       {@code [BEGIN, ..., COMMIT]} at the end of the run ({@link #release}). <b>Nothing is
 *       held past the {@code run()} that received it</b> — an OP holding events across
 *       {@code run()} is invisible to the checkpoint marker, which records the source's read
 *       position past them, and a kill loses them (§9). A partial batch is also released by an
 *       input boundary (which closes the batch before disengaging), by {@code flush()} and by
 *       {@code close()}. Every held event keeps the position of its own source event; the
 *       markers carry the first and last source's; a released batch is monotone in position, so
 *       the endpoint's prefix filter (§3.2) drops exactly what the checkpoint covers on replay.
 *       Batch markers carry a {@code TxnID} only when every source in the batch had the same
 *       one.</li>
 * </ul>
 *
 * <p><b>Three rules, all decided here:</b></p>
 * <ol>
 *   <li><b>Never nest.</b> Transactions do not nest (§8.8), so the moment a boundary is seen on the
 *       INPUT — {@code BEGIN}, {@code COMMIT} or {@code ROLLBACK} in {@code metadata.OperationName}
 *       — wrapping stops for the life of the component. The input is observed on every call, so a
 *       boundary that arrives after wrapping has started still disengages it. An emitted list that
 *       itself contains a boundary is also left alone.</li>
 *   <li><b>Wrap every group, not only fan-outs.</b> A bare event between two synthetic
 *       transactions is committed only by the NEXT {@code COMMIT} the writer sees: a writer set
 *       to commit on boundaries has its count and timer commits off, and one under a group
 *       commit policy refuses while a transaction is open. Wrapping a singleton costs two marker events;
 *       leaving it bare strands it in an open transaction until the next fan-out happens to
 *       arrive.</li>
 *   <li><b>Whether recovery is on does not change the decision.</b> Without recovery there is no
 *       replay to protect, but a writer configured to commit on boundaries still needs them —
 *       see {@code AbstractOpenProcessorApp#transactionSettings} for the stall that gating
 *       on recovery produced. The property alone decides.</li>
 * </ol>
 *
 * <p><b>What a marker looks like, and why so little.</b> The stock writer resolves a handler by
 * {@code OperationName + CatalogObjectType + PK_UPDATE} (verified: {@code DBOperationHandler
 * .getDefaultHandlerKey}), registered as {@code "COMMITnullnull"}, so a marker that copied a
 * source UPDATE's {@code PK_UPDATE} would key to {@code "COMMITnullTRUE"} and land on the
 * unsupported-operation handler. Metadata therefore carries {@code OperationName} and, when the
 * source has one, {@code TxnID}; nothing else. {@code TableName} is ABSENT rather than null:
 * a downstream OP that guards on {@code containsKey} would dereference a present-but-null value.
 * {@code data}, {@code before} and {@code typeUUID} are null, as on the platform's own markers.
 * Userdata carries the {@code TransactionBoundary}/{@code TransactionBoundaryType} pair, plus
 * {@value #BOUNDARY_SOURCE_KEY} naming the component, so a synthetic marker is distinguishable
 * from a reader's.</p>
 *
 * <p><b>The writer configuration this expects:</b> commit only on the source's transaction
 * boundaries, with the writer's own count and interval commits off — for the stock writer,
 * {@code PreserveSourceTransactionBoundary: true} with {@code CommitPolicy: '-1'}. A count-based
 * commit policy under Preserve builds a group commit policy whose flush commits what has been
 * EXECUTED without executing what is still STAGED, so a table-interleaved group can be
 * half-committed at its own {@code COMMIT}; it is safe only with a statement batch of one. See
 * the design doc's §8.1.</p>
 *
 * <p><b>Grouping by table</b> ({@value #GROUP_BY_TABLE_PROPERTY}, batch scope only) releases a
 * batch table by table — the declared order first, then first appearance, source order within a
 * table — because a writer whose statement batch is per table breaks it at every table change:
 * an interleaved fan-out executes at batch size one whatever its batch policy says. A non-DML
 * event (a DDL announcement) is a barrier: the events before it are grouped among themselves, it
 * goes out in place, the events after it are grouped among themselves, so an announcement still
 * precedes its table's data. A grouped release is NOT monotone in position, and the endpoint's
 * replay filter retires on the first accepted event (§3.2): a replayed batch whose boundaries
 * differ from the original's applies the later tables' already-committed rows again, and a
 * target that commits inside a batch loses the rest of that table's run on a STOP. Under
 * recovery it is therefore sound only when every target commits and acknowledges only on these
 * markers, no CQ in between filters them, the window feeding the OP is count-only (so a replay
 * rebuilds the same batches), and every other target on the application acknowledges only at
 * batch boundaries too — none of which this class can verify. The shell warns at start, or
 * refuses under {@value #ON_UNSAFE_RECOVERY_PROPERTY}{@code : 'halt'}.</p>
 *
 * <p>Not thread-safe; used only from the {@code run()} loop, which is serial per component.</p>
 */
public final class SyntheticTransactionBoundaries {

    /** The unit one synthetic pair wraps: {@code none}, {@code event} or {@code batch}. Read by {@code AbstractOpenProcessorApp}. */
    public static final String SCOPE_PROPERTY = "TransactionScope";

    /** Deprecated alias: {@code true} is {@value #SCOPE_PROPERTY}{@code : 'event'}. */
    public static final String PER_SOURCE_EVENT_PROPERTY = "TransactionPerSourceEvent";

    /** Release a batch table by table rather than in arrival order; default false. Batch scope only. */
    public static final String GROUP_BY_TABLE_PROPERTY = "GroupBatchByTable";

    /** What grouping under recovery does at start: {@code warn} (default) or {@code halt}. */
    public static final String ON_UNSAFE_RECOVERY_PROPERTY = "OnUnsafeRecovery";

    /** Removed: the window batch is the transaction. Still refused by name so an old app fails clearly. */
    public static final String REMOVED_BATCH_SIZE_PROPERTY = "TransactionBatchSize";

    /** Userdata key naming the component that minted a synthetic marker. */
    public static final String BOUNDARY_SOURCE_KEY = "TransactionBoundarySource";

    /** The unit one synthetic transaction wraps; {@link #NONE} is no synthetic transactions. */
    public enum Scope {
        NONE, EVENT, BATCH
    }

    /** What grouping under recovery does at start. */
    public enum OnUnsafeRecovery {
        WARN, HALT
    }

    /** An event to send with the recovery position of the source event it came from. */
    public static final class Positioned {
        private final WAEvent event;
        private final ImmutableStemma position;

        public Positioned(WAEvent event, ImmutableStemma position) {
            this.event = event;
            this.position = position;
        }

        public WAEvent event() {
            return event;
        }

        public ImmutableStemma position() {
            return position;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (o == null || getClass() != o.getClass()) return false;
            Positioned that = (Positioned) o;
            return Objects.equals(event, that.event) && Objects.equals(position, that.position);
        }

        @Override
        public int hashCode() {
            return Objects.hash(event, position);
        }

        @Override
        public String toString() {
            return "Positioned[event=" + event + ", position=" + position + "]";
        }
    }

    /**
     * The transaction settings, parsed once from the TQL properties. {@code viaDeprecatedAlias}
     * says {@value #PER_SOURCE_EVENT_PROPERTY} was set, so the shell can say so once.
     */
    public static final class Settings {

        public static final Settings OFF = new Settings(Scope.NONE, false, OnUnsafeRecovery.WARN, false);

        private final Scope scope;
        private final boolean groupByTable;
        private final OnUnsafeRecovery onUnsafeRecovery;
        private final boolean viaDeprecatedAlias;

        public Settings(Scope scope, boolean groupByTable, OnUnsafeRecovery onUnsafeRecovery,
                boolean viaDeprecatedAlias) {
            Objects.requireNonNull(scope, SCOPE_PROPERTY);
            Objects.requireNonNull(onUnsafeRecovery, ON_UNSAFE_RECOVERY_PROPERTY);
            if (groupByTable && scope != Scope.BATCH) {
                throw new IllegalArgumentException(GROUP_BY_TABLE_PROPERTY + " needs " + SCOPE_PROPERTY
                        + ": 'batch': at event scope one source event's output is one transaction and there"
                        + " is nothing to group");
            }
            this.scope = scope;
            this.groupByTable = groupByTable;
            this.onUnsafeRecovery = onUnsafeRecovery;
            this.viaDeprecatedAlias = viaDeprecatedAlias;
        }

        public Scope scope() {
            return scope;
        }

        public boolean groupByTable() {
            return groupByTable;
        }

        public OnUnsafeRecovery onUnsafeRecovery() {
            return onUnsafeRecovery;
        }

        public boolean viaDeprecatedAlias() {
            return viaDeprecatedAlias;
        }

        /**
         * Reads the properties. An absent or blank {@value #SCOPE_PROPERTY} — blank is what the
         * platform materializes from the template's empty default — falls back to the alias:
         * {@code true} is {@code event}, anything else is {@code none}. Refused: the removed
         * {@value #REMOVED_BATCH_SIZE_PROPERTY}; an unknown scope or verb; the alias set to
         * {@code true} beside an EXPLICIT scope other than {@code event} ({@code none} included);
         * grouping outside batch scope.
         */
        public static Settings fromProps(Map<String, Object> props) {
            String batchSize = text(props, REMOVED_BATCH_SIZE_PROPERTY);
            if (!batchSize.isEmpty()) {
                throw new IllegalArgumentException(REMOVED_BATCH_SIZE_PROPERTY + " is no longer a property: the window"
                        + " batch is the transaction; the OP does not batch. Use " + SCOPE_PROPERTY + ": 'batch'"
                        + " behind a count-only window (KEEP n ROWS) and remove " + REMOVED_BATCH_SIZE_PROPERTY);
            }
            boolean alias = Boolean.parseBoolean(text(props, PER_SOURCE_EVENT_PROPERTY));
            String scopeText = text(props, SCOPE_PROPERTY);
            Scope scope;
            if (scopeText.isEmpty()) {
                scope = alias ? Scope.EVENT : Scope.NONE;
            } else {
                scope = parse(Scope.class, SCOPE_PROPERTY, scopeText);
                if (alias && scope != Scope.EVENT) {
                    throw new IllegalArgumentException(PER_SOURCE_EVENT_PROPERTY + ": true and " + SCOPE_PROPERTY
                            + ": '" + scopeText + "' disagree. " + PER_SOURCE_EVENT_PROPERTY + " is the deprecated"
                            + " form of " + SCOPE_PROPERTY + ": 'event'; remove it");
                }
            }
            String verb = text(props, ON_UNSAFE_RECOVERY_PROPERTY);
            OnUnsafeRecovery onUnsafe = verb.isEmpty() ? OnUnsafeRecovery.WARN
                    : parse(OnUnsafeRecovery.class, ON_UNSAFE_RECOVERY_PROPERTY, verb);
            return new Settings(scope, Boolean.parseBoolean(text(props, GROUP_BY_TABLE_PROPERTY)), onUnsafe, alias);
        }

        public boolean enabled() {
            return scope != Scope.NONE;
        }

        public boolean batchScoped() {
            return scope == Scope.BATCH;
        }

        private static String text(Map<String, Object> props, String key) {
            return Objects.toString(props.get(key), "").trim();
        }

        private static <E extends Enum<E>> E parse(Class<E> kind, String property, String value) {
            try {
                return Enum.valueOf(kind, value.toUpperCase(Locale.ROOT));
            } catch (IllegalArgumentException e) {
                List<String> allowed = new ArrayList<>();
                for (E c : kind.getEnumConstants()) {
                    allowed.add(c.name().toLowerCase(Locale.ROOT));
                }
                throw new IllegalArgumentException(property + " must be one of " + allowed + ": '" + value + "'", e);
            }
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) return true;
            if (o == null || getClass() != o.getClass()) return false;
            Settings settings = (Settings) o;
            return groupByTable == settings.groupByTable
                    && viaDeprecatedAlias == settings.viaDeprecatedAlias
                    && scope == settings.scope
                    && onUnsafeRecovery == settings.onUnsafeRecovery;
        }

        @Override
        public int hashCode() {
            return Objects.hash(scope, groupByTable, onUnsafeRecovery, viaDeprecatedAlias);
        }

        @Override
        public String toString() {
            return "Settings[scope=" + scope + ", groupByTable=" + groupByTable
                    + ", onUnsafeRecovery=" + onUnsafeRecovery
                    + ", viaDeprecatedAlias=" + viaDeprecatedAlias + "]";
        }
    }

    private final String component;
    private final Logger logger;
    private final boolean groupByTable;
    private final List<String> tableOrder;
    private boolean inputBoundarySeen;
    private boolean engaged;

    // The open batch: what each absorbed source event produced, in arrival order.
    private final List<Positioned> pending = new ArrayList<>();
    private int pendingSources;
    private WAEvent firstSource;
    private ImmutableStemma firstPosition;
    private WAEvent lastSource;
    private ImmutableStemma lastPosition;
    private Object sharedTxnId;
    private boolean txnIdShared;

    /**
     * @param component the OP's qualified name, stamped into every marker's userdata
     * @param logger    may be null; the engage/disengage lines are then not written
     */
    public SyntheticTransactionBoundaries(String component, Logger logger) {
        this(component, logger, false, List.of());
    }

    /**
     * @param groupByTable release a batch table by table (see the class comment)
     * @param tableOrder   the tables a grouped release puts first, in this order; others follow in
     *                     first-appearance order
     */
    public SyntheticTransactionBoundaries(String component, Logger logger, boolean groupByTable,
            List<String> tableOrder) {
        this.component = component == null ? "OpenProcessor" : component;
        this.logger = logger;
        this.groupByTable = groupByTable;
        this.tableOrder = tableOrder == null ? List.of() : List.copyOf(tableOrder);
    }

    public boolean groupByTable() {
        return groupByTable;
    }

    /** True while a batch is open, i.e. at least one event is held. */
    public boolean hasPending() {
        return !pending.isEmpty();
    }

    /** Source events absorbed into the open batch, including those that produced nothing. */
    public int pendingSourceEvents() {
        return pendingSources;
    }

    /**
     * The batch-scoped form of {@link #apply}: records {@code emitted} (each event with
     * {@code position}) against the open batch and returns what is to be sent NOW — nothing while
     * the batch is open. An input boundary, or an output that carries one, first closes the open
     * batch and is then forwarded as-is (positioned with {@code position}). Once a boundary has
     * been seen on the input nothing is ever held or wrapped again. The shell calls
     * {@link #release()} at the end of every {@code run()}, so a batch never outlives the
     * platform batch that fed it.
     */
    public List<Positioned> absorb(WAEvent source, ImmutableStemma position, List<WAEvent> emitted) {
        List<Positioned> out = new ArrayList<>();
        if (source != null && !inputBoundarySeen
                && TransactionMetadata.boundaryOf(source.metadata) != TransactionMetadata.Boundary.NONE) {
            out.addAll(release());
            inputBoundarySeen = true;
            if (engaged && logger != null) {
                logger.logAlways(() -> component + ": a transaction boundary arrived on the input stream;"
                        + " synthetic BEGIN/COMMIT wrapping is now OFF for the life of this component"
                        + " (boundaries do not nest). The writer commits on the source's boundaries"
                        + " from here.");
            }
        }
        if (inputBoundarySeen) {
            addPositioned(out, emitted, position);
            return out;
        }
        if (emitted != null) {
            for (WAEvent e : emitted) {
                if (e != null && TransactionMetadata.boundaryOf(e.metadata) != TransactionMetadata.Boundary.NONE) {
                    out.addAll(release());
                    addPositioned(out, emitted, position);
                    return out;
                }
            }
        }
        if (pendingSources == 0) {
            firstSource = source;
            firstPosition = position;
            sharedTxnId = txnIdOf(source);
            txnIdShared = true;
        } else if (txnIdShared && !Objects.equals(sharedTxnId, txnIdOf(source))) {
            txnIdShared = false;
        }
        pendingSources++;
        lastSource = source;
        lastPosition = position;
        boolean wasEmpty = pending.isEmpty();
        addPositioned(pending, emitted, position);
        if (wasEmpty && !pending.isEmpty()) {
            engage(true); // said when the first event is held, not at the release: the operator sees it at once
        }
        return out;
    }

    /**
     * Closes the open batch: {@code [BEGIN, held events..., COMMIT]}, regrouped by table when so
     * configured; empty when nothing is held. Called by the shell at the end of every
     * {@code run()}, on {@code flush()} and on {@code close()}.
     */
    public List<Positioned> release() {
        if (pending.isEmpty()) {
            pendingSources = 0;
            return List.of();
        }
        Object txnId = txnIdShared ? sharedTxnId : null;
        List<Positioned> out = new ArrayList<>(pending.size() + 2);
        out.add(new Positioned(marker("BEGIN", firstSource, txnId), firstPosition));
        out.addAll(groupByTable ? grouped(pending) : pending);
        out.add(new Positioned(marker("COMMIT", lastSource, txnId), lastPosition));
        pending.clear();
        pendingSources = 0;
        firstSource = lastSource = null;
        firstPosition = lastPosition = null;
        return out;
    }

    /**
     * Drops the open batch without emitting it. For the one case where events ARE held across
     * {@code run()}: the run that absorbed them threw under the fail-the-batch error model, so
     * whatever delivers that input again (a restart under recovery re-reads from the checkpoint)
     * would absorb them a second time.
     */
    public int discard() {
        int dropped = pending.size();
        pending.clear();
        pendingSources = 0;
        firstSource = lastSource = null;
        firstPosition = lastPosition = null;
        return dropped;
    }

    private void engage(boolean batchScoped) {
        if (engaged) {
            return;
        }
        engaged = true;
        if (logger != null) {
            final String width = batchScoped
                    ? "each platform batch's output" + (groupByTable ? ", grouped by table" : "")
                    : "each source event's output";
            logger.logAlways(() -> component + ": the input stream carries no transaction"
                    + " boundaries; wrapping " + width + " in synthetic"
                    + " BEGIN/COMMIT so events sharing one recovery position commit atomically."
                    + " The target must set PreserveSourceTransactionBoundary: true and"
                    + " CommitPolicy: '-1', and no CQ between here and it may filter BEGIN/COMMIT."
                    + " Verify transaction boundaries before using fan-out with recovery.");
        }
    }

    /**
     * Declared tables first, in their order; then the rest by first appearance; source order
     * within. A non-DML event (a DDL announcement, anything that is not a row operation) is a
     * barrier: the run before it is grouped among itself, it is emitted in place, the run after it
     * is grouped among itself — so an announcement still precedes its table's rows.
     */
    private List<Positioned> grouped(List<Positioned> events) {
        List<Positioned> out = new ArrayList<>(events.size());
        List<Positioned> segment = new ArrayList<>();
        for (Positioned p : events) {
            if (isRowOperation(p.event())) {
                segment.add(p);
                continue;
            }
            out.addAll(groupSegment(segment));
            segment.clear();
            out.add(p);
        }
        out.addAll(groupSegment(segment));
        return out;
    }

    private List<Positioned> groupSegment(List<Positioned> segment) {
        if (segment.size() < 2) {
            return new ArrayList<>(segment);
        }
        Map<String, List<Positioned>> byTable = new LinkedHashMap<>();
        for (String t : tableOrder) {
            byTable.put(t, new ArrayList<>());
        }
        for (Positioned p : segment) {
            byTable.computeIfAbsent(tableOf(p.event()), k -> new ArrayList<>()).add(p);
        }
        List<Positioned> out = new ArrayList<>(segment.size());
        for (List<Positioned> run : byTable.values()) {
            out.addAll(run);
        }
        return out;
    }

    private static final java.util.Set<String> ROW_OPERATIONS = java.util.Set.of("INSERT", "UPDATE", "DELETE",
            "SELECT", "PK_UPDATE");

    private static boolean isRowOperation(WAEvent e) {
        Object op = e == null || e.metadata == null ? null : e.metadata.get(TransactionMetadata.OPERATION_NAME);
        return op != null && ROW_OPERATIONS.contains(op.toString().toUpperCase(Locale.ROOT));
    }

    private static String tableOf(WAEvent e) {
        Object t = e == null || e.metadata == null ? null : e.metadata.get("TableName");
        return t == null ? "" : t.toString();
    }

    private static Object txnIdOf(WAEvent source) {
        return source == null || source.metadata == null ? null : source.metadata.get(TransactionMetadata.TXN_ID);
    }

    private static void addPositioned(List<Positioned> to, List<WAEvent> events, ImmutableStemma position) {
        if (events == null) {
            return;
        }
        for (WAEvent e : events) {
            if (e != null) {
                to.add(new Positioned(e, position));
            }
        }
    }

    /**
     * The event-scoped form: observes {@code source} and returns what should actually be sent
     * for it: {@code emitted} unchanged once a boundary has been seen on the input (or when there
     * is nothing to send), otherwise {@code [BEGIN, emitted..., COMMIT]}.
     *
     * <p>Call once per source event with everything it produced — including the error-model
     * passthrough, which is a single-event group like any other.</p>
     */
    public List<WAEvent> apply(WAEvent source, List<WAEvent> emitted) {
        if (source != null && !inputBoundarySeen
                && TransactionMetadata.boundaryOf(source.metadata) != TransactionMetadata.Boundary.NONE) {
            inputBoundarySeen = true;
            if (engaged && logger != null) {
                logger.logAlways(() -> component + ": a transaction boundary arrived on the input stream;"
                        + " synthetic BEGIN/COMMIT wrapping is now OFF for the life of this component"
                        + " (boundaries do not nest). The writer commits on the source's boundaries"
                        + " from here.");
            }
        }
        if (inputBoundarySeen || emitted == null || emitted.isEmpty()) {
            return emitted;
        }
        for (WAEvent e : emitted) {
            if (e != null && TransactionMetadata.boundaryOf(e.metadata) != TransactionMetadata.Boundary.NONE) {
                return emitted;
            }
        }
        engage(false);
        List<WAEvent> out = new ArrayList<>(emitted.size() + 2);
        out.add(marker("BEGIN", source));
        out.addAll(emitted);
        out.add(marker("COMMIT", source));
        return out;
    }

    /** True once a {@code BEGIN}, {@code COMMIT} or {@code ROLLBACK} has been seen on the input. */
    public boolean inputBoundarySeen() {
        return inputBoundarySeen;
    }

    /** True once at least one group has been wrapped — under batch scope, once the first event was held. */
    public boolean engaged() {
        return engaged;
    }

    /**
     * A synthetic marker for {@code operationName}, borrowing {@code source}'s {@code sourceUUID}
     * and {@code TxnID} (raw, not coerced — PostgreSQL's is an {@code Integer}) and nothing else.
     */
    public WAEvent marker(String operationName, WAEvent source) {
        return marker(operationName, source, txnIdOf(source));
    }

    /** As {@link #marker(String, WAEvent)} with the transaction id given: null means none. */
    private WAEvent marker(String operationName, WAEvent source, Object txnId) {
        WAEvent event = new WAEvent(0, source == null ? null : source.sourceUUID);
        // The (int, UUID) constructor leaves both maps null.
        event.metadata = new HashMap<>();
        event.userdata = new HashMap<>();
        event.metadata.put(TransactionMetadata.OPERATION_NAME, operationName);
        if (txnId != null) {
            event.metadata.put(TransactionMetadata.TXN_ID, txnId);
        }
        event.userdata.put("TransactionBoundary", "True");
        event.userdata.put("TransactionBoundaryType", operationName);
        event.userdata.put(BOUNDARY_SOURCE_KEY, component);
        event.data = null;
        event.before = null;
        event.typeUUID = null;
        return event;
    }
}
