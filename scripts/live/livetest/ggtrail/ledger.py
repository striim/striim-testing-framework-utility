from __future__ import annotations
import random

# The referential ledger (spec §6) -- the state that makes R4-R9 true at EVERY PREFIX of
# the op stream, not merely at the end.
#
# Two structures beyond the obvious pk -> row map, both there for the same reason: picking
# an update/delete target must stay O(1) at any scale, or a 100M-op load run degenerates.
#
#   picks[] + pick_index{}   uniform random pick over LIVE rows, swap-remove on delete.
#   zero[]  + zero_index{}   the same trick over live rows with refcount 0 -- the only rows
#                            a delete may target (deleting a referenced parent would orphan
#                            a live child, breaking R4 mid-stream).
#
# Memory is O(live rows). The op STREAM is never stored, so op count is unbounded (R14, D7).


class LedgerError(RuntimeError):
    """An attempted transition the ledger's invariants forbid (an engine bug, not config)."""


class TableLedger:
    """Live-row state for ONE table. Refcounts on a row count the live CHILD rows that
    reference it, so they are incremented/decremented by the child's ledger operations.
    """

    def __init__(self, name: str):
        self.name = name
        self.live: dict[tuple, dict] = {}
        self.refcount: dict[tuple, int] = {}
        self._picks: list[tuple] = []
        self._pick_index: dict[tuple, int] = {}
        self._zero: list[tuple] = []
        self._zero_index: dict[tuple, int] = {}
        self.inserted = 0  # ever-inserted count (never decremented)
        self.deleted = 0

    # --- pick-list plumbing -------------------------------------------------------

    @staticmethod
    def _add(items: list, index: dict, pk: tuple) -> None:
        if pk in index:
            return
        index[pk] = len(items)
        items.append(pk)

    @staticmethod
    def _remove(items: list, index: dict, pk: tuple) -> None:
        i = index.pop(pk, None)
        if i is None:
            return
        last = items.pop()
        if i < len(items):  # swap-remove: move the tail into the hole
            items[i] = last
            index[last] = i

    # --- transitions --------------------------------------------------------------

    def insert(self, pk: tuple, row: dict) -> None:
        if pk in self.live:
            raise LedgerError(f"{self.name}: insert of already-live key {pk!r}")
        self.live[pk] = row
        self.refcount[pk] = 0
        self._add(self._picks, self._pick_index, pk)
        self._add(
            self._zero, self._zero_index, pk
        )  # a brand new row is referenced by nobody
        self.inserted += 1

    def update(self, pk: tuple, row: dict) -> None:
        if pk not in self.live:
            raise LedgerError(f"{self.name}: update of non-live key {pk!r}")
        # Replaces values in place: an update must never change the live-set SIZE (R9).
        self.live[pk] = row

    def delete(self, pk: tuple) -> dict:
        if pk not in self.live:
            raise LedgerError(f"{self.name}: delete of non-live key {pk!r}")
        if self.refcount.get(pk, 0) != 0:
            raise LedgerError(
                f"{self.name}: delete of key {pk!r} still referenced by "
                f"{self.refcount[pk]} live child row(s)"
            )
        row = self.live.pop(pk)
        self.refcount.pop(pk, None)
        self._remove(self._picks, self._pick_index, pk)
        self._remove(self._zero, self._zero_index, pk)
        self.deleted += 1
        return row

    # --- reference counting -------------------------------------------------------

    def incref(self, pk: tuple) -> None:
        if pk not in self.live:
            raise LedgerError(f"{self.name}: incref of non-live key {pk!r}")
        count = self.refcount[pk] = self.refcount.get(pk, 0) + 1
        if count == 1:
            self._remove(self._zero, self._zero_index, pk)

    def decref(self, pk: tuple) -> None:
        current = self.refcount.get(pk)
        if current is None:
            # The parent is already gone -- impossible while the child was live, so this
            # signals a bookkeeping bug rather than a legitimate race.
            raise LedgerError(f"{self.name}: decref of unknown key {pk!r}")
        if current <= 0:
            raise LedgerError(f"{self.name}: decref below zero for key {pk!r}")
        self.refcount[pk] = current - 1
        if current - 1 == 0 and pk in self.live:
            self._add(self._zero, self._zero_index, pk)

    # --- sampling -----------------------------------------------------------------

    def pick(self, rng: random.Random) -> tuple:
        if not self._picks:
            raise LedgerError(f"{self.name}: pick from an empty live set")
        return self._picks[rng.randrange(len(self._picks))]

    def deletable_pick(self, rng: random.Random) -> tuple | None:
        """A uniformly chosen live row with refcount 0, or None when every live row is
        referenced (the engine's cue to fall back to an update -- spec §7.2)."""
        if not self._zero:
            return None
        return self._zero[rng.randrange(len(self._zero))]

    # --- introspection ------------------------------------------------------------

    def row(self, pk: tuple) -> dict:
        return self.live[pk]

    def deletable_count(self) -> int:
        return len(self._zero)

    def rows_sorted(self) -> list[tuple]:
        # Sorted by PK for the expected/state golden. PK tuples can mix int and str across
        # tables but never WITHIN one table's key, so a repr key sorts safely either way.
        return sorted(self.live.items(), key=lambda kv: repr(kv[0]))

    def __len__(self) -> int:
        return len(self.live)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TableLedger {self.name} live={len(self.live)} deletable={len(self._zero)}>"
