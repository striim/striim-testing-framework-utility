from __future__ import annotations
import random
from dataclasses import dataclass, field

from . import values as valuegen
from .keygen import make_keygen
from .ledger import TableLedger
from .model import Schema, Table

# The workload engine (spec §7) -- a deterministic stream of Ops over a validated Schema.
#
# Everything the requirements call for is proven HERE, in pure Python, with no trail bytes
# in sight: FKs point at rows that are live at emit time (R4/R5), updates hit rows that
# were inserted and not deleted (R7) and change something without growing the live set
# (R9), deletes hit live unreferenced rows exactly once (R8), and keys are never reissued
# (R3). runner.py only transcribes this stream.
#
# ops() is a GENERATOR and the only bounded buffer in it is one transaction group, so the
# total op count is limited by nothing but patience (R14, D7).

KINDS = ("insert", "update", "delete")
TXN_PARTS = ("SOLE", "BEGIN", "MIDDLE", "END")

_RESAMPLE_ATTEMPTS = 32


class WorkloadError(ValueError):
    """A workload configuration that does not fit its schema."""


@dataclass
class Op:
    seq: int  # 1-based position in the whole stream
    txn_id: int  # 1-based transaction number
    txn_part: str  # SOLE | BEGIN | MIDDLE | END
    table: str
    kind: str  # insert | update | delete
    before: dict | None  # pre-image: None for insert
    after: dict | None  # post-image: None for delete

    @property
    def image(self) -> dict:
        """The row image this op puts on the wire -- after for insert/update, before for
        delete. Layer 0's write_delete encodes the before-image, write_update the after.
        """
        return self.after if self.kind != "delete" else self.before


@dataclass
class WorkloadSpec:
    seed: int = 0
    initial_rows: dict = field(default_factory=dict)  # per-table Phase-A inserts (R11)
    ops: int = 0  # Phase-B mixed op count (R14)
    mix: dict = field(default_factory=lambda: {"insert": 1.0})  # (R12)
    table_weights: dict = field(
        default_factory=dict
    )  # relative pick frequency; default 1.0
    txn_ops: tuple = (1, 1)  # ops per transaction, uniform range
    keys: object = "sequential"  # str, or {table: str} (R13)


class WorkloadEngine:
    def __init__(self, schema: Schema, spec: WorkloadSpec):
        self.schema = schema.validate()
        self.spec = spec
        self.rng = random.Random(spec.seed)
        self.order: list[Table] = self.schema.topo_order()
        self.ledgers: dict[str, TableLedger] = {
            t.name: TableLedger(t.name) for t in self.order
        }

        self._check_config()
        self.keygens = {
            t.name: {
                c.name: make_keygen(self._key_kind(t.name), self.rng)
                for c in t.pk_columns
            }
            for t in self.order
        }
        # Table pick weights, resolved once into the parallel lists rng.choices wants.
        self._weight_names = [t.name for t in self.order]
        self._weights = [
            float(spec.table_weights.get(n, 1.0)) for n in self._weight_names
        ]
        self._mix_kinds = [k for k in KINDS if float(spec.mix.get(k, 0.0)) > 0.0]
        self._mix_weights = [float(spec.mix[k]) for k in self._mix_kinds]

        self.fallbacks: dict[str, int] = {}
        self.counts: dict[str, int] = {k: 0 for k in KINDS}
        self.table_counts: dict[str, dict[str, int]] = {
            t.name: {k: 0 for k in KINDS} for t in self.order
        }
        self.emitted = 0
        self.transactions = 0

    # --- configuration ------------------------------------------------------------

    def _check_config(self) -> None:
        known = set(self.ledgers)
        for label, mapping in (
            ("initial_rows", self.spec.initial_rows),
            ("table_weights", self.spec.table_weights),
        ):
            for name in mapping:
                if str(name).upper() not in known:
                    raise WorkloadError(
                        f"workload {label} names unknown table {name!r}; schema has {sorted(known)}"
                    )
        for kind, weight in self.spec.mix.items():
            if kind not in KINDS:
                raise WorkloadError(
                    f"workload mix has unknown op {kind!r}; known ops: {list(KINDS)}"
                )
            if float(weight) < 0:
                raise WorkloadError(
                    f"workload mix weight for {kind!r} must be >= 0, got {weight}"
                )
        if self.spec.ops > 0 and sum(float(w) for w in self.spec.mix.values()) <= 0:
            raise WorkloadError(
                "workload mix has no positive weight, so no op can be chosen"
            )
        if any(float(w) < 0 for w in self.spec.table_weights.values()):
            raise WorkloadError("workload table_weights must all be >= 0")
        if (
            self.spec.ops > 0
            and self.order
            and sum(float(self.spec.table_weights.get(t.name, 1.0)) for t in self.order)
            <= 0
        ):
            raise WorkloadError(
                "workload table_weights sum to 0, so no table can be chosen"
            )
        lo, hi = self.spec.txn_ops
        if lo < 1 or hi < lo:
            raise WorkloadError(
                f"workload txn_ops must be [lo, hi] with 1 <= lo <= hi, got {[lo, hi]}"
            )

    def _key_kind(self, table: str) -> str:
        keys = self.spec.keys
        if isinstance(keys, dict):
            # Per-table override; unlisted tables keep the default strategy.
            return str(
                {str(k).upper(): v for k, v in keys.items()}.get(table, "sequential")
            )
        return str(keys)

    # --- public stream ------------------------------------------------------------

    def ops(self):
        """Yield the whole workload as Op records. Streaming: only one transaction group
        (at most txn_ops[1] raw ops) is ever held in memory."""
        seq = 0
        txn_id = 0
        for group in self._groups():
            txn_id += 1
            last = len(group) - 1
            for i, (table, kind, before, after) in enumerate(group):
                if last == 0:
                    part = "SOLE"
                elif i == 0:
                    part = "BEGIN"
                elif i == last:
                    part = "END"
                else:
                    part = "MIDDLE"
                seq += 1
                yield Op(seq, txn_id, part, table, kind, before, after)
            self.transactions = txn_id
            self.emitted = seq

    # --- transaction grouping -----------------------------------------------------

    def _groups(self):
        # The group SIZE is drawn before the group's ops are generated, so the rng call
        # order (and therefore the whole stream) is fixed by the seed alone.
        raw = self._raw_ops()
        lo, hi = self.spec.txn_ops
        while True:
            want = self.rng.randint(lo, hi)
            group = []
            for _ in range(want):
                try:
                    group.append(next(raw))
                except StopIteration:
                    break
            if not group:
                return
            yield group

    def _raw_ops(self):
        # Phase A -- initial load in topo order, so every child's parents are already
        # populated when the child's FK sampling runs (R5).
        for table in self.order:
            for _ in range(int(self.spec.initial_rows.get(table.name, 0))):
                yield self._do_insert(table)
        # Phase B -- mixed ops.
        for _ in range(int(self.spec.ops)):
            yield self._mixed_op()

    # --- op selection (the fallback ladder, spec §7.2) ------------------------------

    def _mixed_op(self):
        table = self.schema.table(
            self.rng.choices(self._weight_names, self._weights)[0]
        )
        kind = self.rng.choices(self._mix_kinds, self._mix_weights)[0]

        if kind in ("update", "delete") and len(self.ledgers[table.name]) == 0:
            kind = self._fallback("empty_table_to_insert", "insert")
        if kind == "delete" and self.ledgers[table.name].deletable_count() == 0:
            # Every live row is referenced by a live child; deleting one would orphan it.
            kind = self._fallback("all_referenced_delete_to_update", "update")
        if kind == "update" and not self._can_update(table):
            kind = self._fallback("nothing_to_mutate_update_to_insert", "insert")

        if kind == "insert":
            return self._do_insert(self._insertable(table))
        if kind == "update":
            return self._do_update(table)
        return self._do_delete(table)

    def _fallback(self, reason: str, kind: str) -> str:
        self.fallbacks[reason] = self.fallbacks.get(reason, 0) + 1
        return kind

    def _insertable(self, table: Table) -> Table:
        """Redirect an insert up the FK graph while a REQUIRED parent has no live rows --
        inserting the child first would have no valid parent to point at (R4)."""
        for parent_name, cols in table.fk_groups().items():
            if len(self.ledgers[parent_name]) == 0 and not self._group_nullable(cols):
                self._fallback("missing_parent_insert_redirected", "insert")
                return self._insertable(self.schema.table(parent_name))
        return table

    def _can_update(self, table: Table) -> bool:
        # An update must change something (R9). A table whose only non-PK columns are FKs
        # into single-row parents has nothing it could legally change.
        if any(not c.pk and c.fk is None for c in table.columns):
            return True
        return any(len(self.ledgers[p]) >= 2 for p in table.fk_groups())

    # --- transitions ---------------------------------------------------------------

    def _do_insert(self, table: Table):
        fk_values, refs = self._sample_fks(table)
        keys = self.keygens[table.name]
        row = {}
        for c in table.columns:
            if c.pk:
                row[c.name] = keys[c.name].next()
            elif c.fk is not None:
                row[c.name] = fk_values[c.name]
            else:
                row[c.name] = valuegen.generate(c, self.rng)
        pk = tuple(row[n] for n in table.pk_names)
        self.ledgers[table.name].insert(pk, dict(row))
        for parent_name, parent_pk in refs:
            self.ledgers[parent_name].incref(parent_pk)
        self._count(table.name, "insert")
        return (table.name, "insert", None, dict(row))

    def _do_update(self, table: Table):
        ledger = self.ledgers[table.name]
        pk = ledger.pick(self.rng)
        before = dict(ledger.row(pk))
        after = dict(before)

        units = self._mutable_units(table)
        chosen = self.rng.sample(range(len(units)), self.rng.randint(1, len(units)))
        for i in sorted(chosen):
            self._apply_unit(table, units[i], before, after)
        if after == before:
            # The random subset happened to redraw identical values (a narrow domain, or an
            # FK that re-picked its current parent). Force one guaranteed change rather than
            # emitting an update that updates nothing.
            for unit in units:
                if self._apply_unit(table, unit, before, after, force=True):
                    break
        if after == before:  # pragma: no cover - _can_update makes this unreachable
            # Loud rather than silent: emitting an update that changes nothing would break
            # R9 in a way only a downstream diff would notice, and only sometimes.
            raise WorkloadError(
                f"{table.name}: could not produce a changed update image for key {pk!r}"
            )

        ledger.update(pk, dict(after))
        self._count(table.name, "update")
        return (table.name, "update", before, after)

    def _do_delete(self, table: Table):
        ledger = self.ledgers[table.name]
        pk = ledger.deletable_pick(self.rng)
        if pk is None:  # pragma: no cover - ladder guards it
            raise WorkloadError(
                f"{table.name}: no deletable row (every live row is referenced)"
            )
        before = dict(ledger.row(pk))
        ledger.delete(pk)
        for parent_name, parent_pk in self._parent_refs(table, before):
            self.ledgers[parent_name].decref(parent_pk)
        self._count(table.name, "delete")
        return (table.name, "delete", before, None)

    # --- FK sampling ----------------------------------------------------------------

    def _group_nullable(self, cols) -> bool:
        # All-or-nothing: a composite FK is NULL only if every participating column may be.
        return all(c.nullable for c in cols)

    def _parent_pk(self, parent_name: str, cols, row: dict):
        """The parent PK tuple a child row currently references, or None when the FK is
        NULL. Derived from the row's own values -- no hidden per-row reference bookkeeping
        that could drift out of sync with the emitted images."""
        parent = self.schema.table(parent_name)
        by_parent_col = {c.parent_column: row.get(c.name) for c in cols}
        if any(by_parent_col[n] is None for n in parent.pk_names):
            return None
        return tuple(by_parent_col[n] for n in parent.pk_names)

    def _parent_refs(self, table: Table, row: dict) -> list[tuple]:
        out = []
        for parent_name, cols in table.fk_groups().items():
            parent_pk = self._parent_pk(parent_name, cols, row)
            if parent_pk is not None:
                out.append((parent_name, parent_pk))
        return out

    def _sample_fks(self, table: Table):
        """Values for every FK column plus the (parent, pk) pairs to incref."""
        fk_values: dict = {}
        refs: list = []
        for parent_name, cols in table.fk_groups().items():
            value_map, parent_pk = self._sample_group(parent_name, cols, avoid=None)
            fk_values.update(value_map)
            if parent_pk is not None:
                refs.append((parent_name, parent_pk))
        return fk_values, refs

    def _sample_group(self, parent_name: str, cols, avoid, allow_null: bool = True):
        """Pick one live parent row and spread its PK across the child's FK columns.

        Sampling the group as a UNIT is what makes a composite FK point at a row that
        actually exists -- picking each column independently would synthesize a PK tuple
        that was never inserted.

        `allow_null=False` is the forced-change path: an update that MUST change this group
        cannot afford to roll a NULL that happens to match what the column already holds.
        """
        ledger = self.ledgers[parent_name]
        nullable = self._group_nullable(cols)
        if (
            nullable
            and allow_null
            and cols[0].null_rate > 0.0
            and self.rng.random() < cols[0].null_rate
        ):
            return {c.name: None for c in cols}, None
        if len(ledger) == 0:
            if not nullable:  # pragma: no cover - ladder guards it
                raise WorkloadError(
                    f"cannot sample foreign key into {parent_name}: it has no live rows"
                )
            return {c.name: None for c in cols}, None
        parent_pk = ledger.pick(self.rng)
        if avoid is not None and len(ledger) > 1:
            for _ in range(_RESAMPLE_ATTEMPTS):
                if parent_pk != avoid:
                    break
                parent_pk = ledger.pick(self.rng)
        parent = self.schema.table(parent_name)
        by_name = dict(zip(parent.pk_names, parent_pk))
        return {c.name: by_name[c.parent_column] for c in cols}, parent_pk

    # --- update mutation -------------------------------------------------------------

    def _mutable_units(self, table: Table) -> list:
        """The independently-changeable pieces of a row: each plain column on its own, each
        FK group as one unit (its columns must move together to stay referentially valid).
        """
        units: list = []
        for c in table.columns:
            if not c.pk and c.fk is None:
                units.append(("column", c))
        for parent_name, cols in table.fk_groups().items():
            units.append(("fk", parent_name, cols))
        return units

    def _apply_unit(
        self, table: Table, unit, before: dict, after: dict, force: bool = False
    ) -> bool:
        """Mutate one unit of `after` in place. Returns whether anything actually changed.

        PK columns are never units, so an update can never change a row's identity (D5).
        """
        if unit[0] == "column":
            col = unit[1]
            new = valuegen.mutate(col, after.get(col.name), self.rng)
            changed = new != after.get(col.name)
            after[col.name] = new
            return changed

        _, parent_name, cols = unit
        ledger = self.ledgers[parent_name]
        old_pk = self._parent_pk(parent_name, cols, after)
        if len(ledger) == 0 or (force and len(ledger) < 2 and old_pk is not None):
            return False
        value_map, new_pk = self._sample_group(
            parent_name, cols, avoid=old_pk if force else None, allow_null=not force
        )
        if new_pk == old_pk:
            after.update(value_map)
            return False
        # Reference bookkeeping moves with the value: the old parent loses a referrer (and
        # may become deletable again), the new one gains one.
        if old_pk is not None:
            ledger.decref(old_pk)
        if new_pk is not None:
            ledger.incref(new_pk)
        after.update(value_map)
        return True

    # --- accounting -------------------------------------------------------------------

    def _count(self, table: str, kind: str) -> None:
        self.counts[kind] += 1
        self.table_counts[table][kind] += 1

    def live_counts(self) -> dict[str, int]:
        return {name: len(led) for name, led in self.ledgers.items()}
