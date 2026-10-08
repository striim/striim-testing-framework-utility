package com.example.common;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Function;

/**
 * Groups a batch into apply units that <b>never split a source transaction</b>.
 *
 * <p>Splitting is the hazard this class exists to prevent: half a transaction committed at the
 * target is a state no source position describes, and a reader of that table sees an inconsistency
 * the source never had. Merging whole transactions into one apply is the weaker, safe direction —
 * the target still moves between consistent states, just in coarser steps.</p>
 *
 * <h2>Why a run-length scan is enough</h2>
 * Transactions arrive <b>contiguously</b>: measured over the reference capture, 100,000 events
 * carried 2,045 distinct transaction ids in exactly 2,045 runs — no transaction was ever
 * interleaved with another. So grouping needs a single forward pass and no buffering, which is why
 * this class holds no state between calls.
 *
 * <p><b>That is a property of the sources measured, not a guarantee.</b> If a transaction id ever
 * reappears after another has intervened, a run-length scan would split it silently — the exact
 * failure this class exists to prevent. So the scan <b>detects</b> that rather than assuming it
 * away: see {@link Result#interleavedTransactions()}. A caller in {@code TRANSACTION} mode must
 * treat a non-empty set as fatal; the alternative is to keep the guarantee's name while having lost
 * it.</p>
 *
 * <h2>Oversized transactions</h2>
 * A transaction that <b>reaches</b> {@code maxEventsPerGroup} — the check is {@code >=}, so equal
 * counts too — is emitted <b>alone and intact</b> rather
 * than split. That keeps the invariant, and hands the caller a group it can measure against the
 * target's own commit ceiling — Spanner caps a commit by mutation count, and 2.8% of transactions
 * in the reference workload exceed it while carrying 77.5% of the rows. Such a group is reported
 * through {@link Result#oversizedGroups()} so the decision to fail or proceed is the caller's and
 * is taken knowingly.
 */
public final class TransactionGrouper {

    /** One apply unit: whole transactions, in arrival order. */
    public static final class Group<T> {
        private final List<T> items;
        private final int transactionCount;
        private final boolean oversized;

        Group(final List<T> items, final int transactionCount, final boolean oversized) {
            this.items = items;
            this.transactionCount = transactionCount;
            this.oversized = oversized;
        }

        /** The events in this group, in arrival order. */
        public List<T> items()        { return items; }
        /** How many events the group holds, which is what the size target is measured against. */
        public int size()             { return items.size(); }
        /** How many whole source transactions this group merges. */
        public int transactionCount() { return transactionCount; }
        /** True when a single transaction exceeded the size target and was kept intact anyway. */
        public boolean oversized()    { return oversized; }
    }

    /** Groups plus what the caller needs to know about how they were formed. */
    public static final class Result<T> {
        private final List<Group<T>> groups;
        private final Set<String> interleaved;
        private final int oversizedGroups;

        Result(final List<Group<T>> groups, final Set<String> interleaved, final int oversizedGroups) {
            this.groups = groups;
            this.interleaved = interleaved;
            this.oversizedGroups = oversizedGroups;
        }

        /** The groups formed, in order; each is applied as one unit. */
        public List<Group<T>> groups() { return groups; }

        /**
         * Transaction ids seen again after another transaction intervened.
         *
         * <p>Non-empty means the contiguity this scan relies on does not hold for this source, and
         * those transactions <b>have been split</b>. Fatal in {@code TRANSACTION} mode.</p>
         */
        public Set<String> interleavedTransactions() { return interleaved; }

        /** Groups holding one transaction that exceeded the size target. */
        public int oversizedGroups() { return oversizedGroups; }
    }

    private TransactionGrouper() {
    }

    /**
     * Splits {@code items} into groups on transaction boundaries.
     *
     * @param transactionIdOf returns an item's transaction id; null means "no boundary information",
     *                        and such items are grouped purely by size
     * @param maxEventsPerGroup soft target — a single transaction larger than this is still kept whole
     */
    public static <T> Result<T> group(final List<T> items,
                                      final Function<T, String> transactionIdOf,
                                      final int maxEventsPerGroup) {
        if (maxEventsPerGroup <= 0) {
            throw new IllegalArgumentException("maxEventsPerGroup must be > 0, got " + maxEventsPerGroup);
        }
        final List<Group<T>> groups = new ArrayList<>();
        if (items == null || items.isEmpty()) {
            return new Result<>(groups, java.util.Collections.emptySet(), 0);
        }

        final Set<String> seen = new HashSet<>();
        final Set<String> interleaved = new HashSet<>();
        List<T> current = new ArrayList<>();
        int currentTxns = 0;
        int oversized = 0;

        List<T> run = new ArrayList<>();
        String runId = null;
        boolean first = true;

        for (final T item : items) {
            final String id = transactionIdOf.apply(item);
            if (first) {
                runId = id;
                first = false;
            }
            if (!sameId(id, runId)) {
                // The run just ended: place it, then start the next.
                if (runId != null && !seen.add(runId)) {
                    interleaved.add(runId);
                }
                final int[] counters = place(groups, current, run, currentTxns, maxEventsPerGroup);
                current = pickCurrent(groups, current, run, maxEventsPerGroup);
                currentTxns = counters[0];
                oversized += counters[1];
                run = new ArrayList<>();
                runId = id;
            }
            run.add(item);
        }
        if (runId != null && !seen.add(runId)) {
            interleaved.add(runId);
        }
        final int[] counters = place(groups, current, run, currentTxns, maxEventsPerGroup);
        current = pickCurrent(groups, current, run, maxEventsPerGroup);
        oversized += counters[1];
        if (!current.isEmpty()) {
            groups.add(new Group<>(current, counters[0], false));
        }
        return new Result<>(groups, interleaved, oversized);
    }

    /**
     * Adds one completed run to the open group, closing it first when the run would overflow.
     *
     * @return {@code [transactionsNowInOpenGroup, oversizedGroupsAdded]}
     */
    private static <T> int[] place(final List<Group<T>> groups, final List<T> current,
                                   final List<T> run, final int currentTxns, final int max) {
        if (run.isEmpty()) {
            return new int[]{currentTxns, 0};
        }
        if (run.size() >= max) {
            // Bigger than the target on its own. Keep it whole and alone: splitting it would break
            // the never-split invariant, and merging it with neighbours only makes an
            // already-oversized group worse.
            if (!current.isEmpty()) {
                groups.add(new Group<>(new ArrayList<>(current), currentTxns, false));
            }
            groups.add(new Group<>(new ArrayList<>(run), 1, true));
            return new int[]{0, 1};
        }
        if (current.size() + run.size() > max && !current.isEmpty()) {
            groups.add(new Group<>(new ArrayList<>(current), currentTxns, false));
            return new int[]{1, 0};
        }
        return new int[]{currentTxns + 1, 0};
    }

    /** Mirrors {@link #place}'s decision, returning the list the next run should accumulate into. */
    private static <T> List<T> pickCurrent(final List<Group<T>> groups, final List<T> current,
                                           final List<T> run, final int max) {
        if (run.isEmpty()) {
            return current;
        }
        if (run.size() >= max) {
            return new ArrayList<>();
        }
        if (current.size() + run.size() > max && !current.isEmpty()) {
            return new ArrayList<>(run);
        }
        final List<T> merged = new ArrayList<>(current);
        merged.addAll(run);
        return merged;
    }

    private static boolean sameId(final String a, final String b) {
        return a == null ? b == null : a.equals(b);
    }
}
