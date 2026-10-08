from __future__ import annotations
import time

import pytest

from livetest.ggtrail.model import Column, Schema, Table
from livetest.ggtrail.workload import KINDS, WorkloadEngine, WorkloadError, WorkloadSpec

# Tier 1 (spec §10) -- THE core suite. Every requirement about referential consistency is
# proven here by replaying the engine's op stream against an INDEPENDENT shadow ledger (a
# plain dict-of-dicts written from the requirements, deliberately not reusing ledger.py)
# and asserting the invariants AT EVERY OP, not just at the end:
#
#   R3  keys are never reissued -- a pk seen once is never inserted again
#   R4  every FK value points at a row that is live at emit time; a delete never orphans
#   R5  a child only ever references a key that was already generated
#   R7  every update targets a live, previously-inserted row
#   R8  every delete targets a live row, exactly once
#   R9  an update changes >=1 non-PK column and never grows the live set
#   R11 initial_rows honoured per table
#   R12 mix ratios honoured within tolerance, with every fallback accounted for
#   R14 a 100k-op run streams in bounded memory


def _col(name, dtype="int", **kw):
    return Column(name=name, dtype=dtype, **kw)


def three_table_schema() -> Schema:
    return Schema(
        [
            Table(
                "SCOTT.CUSTOMERS",
                [
                    _col("ID", pk=True),
                    _col("NAME", "varchar(50)"),
                    _col("TIER", "char(1)"),
                ],
            ),
            Table(
                "SCOTT.ORDERS",
                [
                    _col("ID", pk=True),
                    _col("CUSTOMER_ID", fk="SCOTT.CUSTOMERS.ID"),
                    _col("AMOUNT", "decimal(10,2)"),
                    _col("PLACED_AT", "timestamp"),
                ],
            ),
            Table(
                "SCOTT.ORDER_ITEMS",
                [
                    _col("ID", pk=True),
                    _col("ORDER_ID", fk="SCOTT.ORDERS.ID"),
                    _col("SKU", "varchar(20)"),
                    _col("QTY", "int"),
                ],
            ),
        ]
    ).validate()


def single_table_schema() -> Schema:
    return Schema(
        [
            Table(
                "SCOTT.T",
                [
                    _col("ID", pk=True),
                    _col("NAME", "varchar(30)"),
                    _col("AMT", "decimal(8,2)"),
                ],
            )
        ]
    ).validate()


def composite_schema() -> Schema:
    return Schema(
        [
            Table(
                "SCOTT.P",
                [
                    _col("REGION", "char(2)", pk=True),
                    _col("ID", pk=True),
                    _col("LABEL", "varchar(20)"),
                ],
            ),
            Table(
                "SCOTT.C",
                [
                    _col("ID", pk=True),
                    _col("P_REGION", "char(2)", fk="SCOTT.P.REGION"),
                    _col("P_ID", fk="SCOTT.P.ID"),
                    _col("NOTE", "varchar(20)"),
                ],
            ),
        ]
    ).validate()


# --- the independent reference interpreter --------------------------------------------------


class ShadowLedger:
    """A from-the-requirements replay of the op stream. Knows nothing about ledger.py."""

    def __init__(self, schema: Schema):
        self.schema = schema
        self.live: dict[str, dict] = {t.name: {} for t in schema.tables}
        self.ever_keys: dict[str, set] = {t.name: set() for t in schema.tables}
        self.deleted: dict[str, set] = {t.name: set() for t in schema.tables}
        self.children: dict[str, list] = {}
        for table in schema.tables:
            for parent, cols in table.fk_groups().items():
                self.children.setdefault(parent, []).append((table.name, cols))

    def pk_of(self, table_name: str, row: dict) -> tuple:
        return tuple(row[n] for n in self.schema.table(table_name).pk_names)

    def parent_refs(self, table_name: str, row: dict) -> list:
        out = []
        for parent_name, cols in self.schema.table(table_name).fk_groups().items():
            parent_pks = self.schema.table(parent_name).pk_names
            by_parent_col = {c.parent_column: row.get(c.name) for c in cols}
            if any(by_parent_col[n] is None for n in parent_pks):
                assert all(c.nullable for c in cols), (
                    f"{table_name}: NULL foreign key on non-nullable column(s) "
                    f"{[c.name for c in cols if not c.nullable]}"
                )
                continue
            out.append((parent_name, tuple(by_parent_col[n] for n in parent_pks)))
        return out

    def apply(self, op) -> None:
        table = self.schema.table(op.table)
        names = [c.name for c in table.columns]

        if op.kind == "insert":
            assert op.before is None and op.after is not None
            assert sorted(op.after) == sorted(
                names
            ), f"op {op.seq}: partial insert image"
            pk = self.pk_of(op.table, op.after)
            # R3: a key means one logical record, ever -- never reissued after a delete.
            assert pk not in self.ever_keys[op.table], f"op {op.seq}: key {pk} reissued"
            self._assert_fks_live(op, op.after)
            self.ever_keys[op.table].add(pk)
            self.live[op.table][pk] = dict(op.after)

        elif op.kind == "update":
            assert op.before is not None and op.after is not None
            pk = self.pk_of(op.table, op.before)
            # R7: the target must be live -- inserted earlier and not since deleted.
            assert (
                pk in self.live[op.table]
            ), f"op {op.seq}: update of non-live key {pk}"
            assert (
                self.live[op.table][pk] == op.before
            ), f"op {op.seq}: stale before-image"
            # D5: identity is immutable; a PK change would be delete+insert.
            assert (
                self.pk_of(op.table, op.after) == pk
            ), f"op {op.seq}: update changed the PK"
            changed = [n for n in names if op.before.get(n) != op.after.get(n)]
            # R9: an update must actually update something, and only non-PK columns.
            assert changed, f"op {op.seq}: update changed nothing"
            assert not set(changed) & set(table.pk_names)
            self._assert_fks_live(op, op.after)
            before_size = len(self.live[op.table])
            self.live[op.table][pk] = dict(op.after)
            # R9: an update modifies an existing record rather than creating one.
            assert len(self.live[op.table]) == before_size

        elif op.kind == "delete":
            assert op.after is None and op.before is not None
            pk = self.pk_of(op.table, op.before)
            # R8: previously inserted and not already deleted.
            assert (
                pk in self.live[op.table]
            ), f"op {op.seq}: delete of non-live key {pk}"
            assert (
                pk not in self.deleted[op.table]
            ), f"op {op.seq}: double delete of {pk}"
            assert (
                self.live[op.table][pk] == op.before
            ), f"op {op.seq}: stale delete image"
            # R4 at this prefix: removing this row must not orphan any LIVE child.
            for child_name, cols in self.children.get(op.table, []):
                for child_pk, child_row in self.live[child_name].items():
                    refs = dict(self.parent_refs(child_name, child_row))
                    assert refs.get(op.table) != pk, (
                        f"op {op.seq}: deleting {op.table}{pk} orphans "
                        f"{child_name}{child_pk}"
                    )
            del self.live[op.table][pk]
            self.deleted[op.table].add(pk)

        else:  # pragma: no cover
            raise AssertionError(f"op {op.seq}: unknown kind {op.kind!r}")

    def _assert_fks_live(self, op, row: dict) -> None:
        # R4/R5: the parent must be live RIGHT NOW, not merely to have existed once.
        for parent_name, parent_pk in self.parent_refs(op.table, row):
            assert (
                parent_pk in self.live[parent_name]
            ), f"op {op.seq}: {op.table} references {parent_name}{parent_pk}, which is not live"


def replay(schema: Schema, ops) -> ShadowLedger:
    shadow = ShadowLedger(schema)
    for op in ops:
        shadow.apply(op)
    return shadow


def run(schema: Schema, spec: WorkloadSpec):
    engine = WorkloadEngine(schema, spec)
    return engine, list(engine.ops())


# --- the invariants, across several schema shapes ---------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [three_table_schema, single_table_schema, composite_schema],
    ids=["three-table", "single-table", "composite-pk"],
)
def test_every_invariant_holds_at_every_op(factory):
    schema = factory()
    initial = {t.name: 15 for t in schema.tables}
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=3,
            initial_rows=initial,
            ops=1500,
            mix={"insert": 0.4, "update": 0.4, "delete": 0.2},
            txn_ops=(1, 4),
        ),
    )
    shadow = replay(schema, ops)
    # The independent replay must land on exactly the engine's own final state (R3).
    for table in schema.tables:
        assert set(shadow.live[table.name]) == set(engine.ledgers[table.name].live)
        assert shadow.live[table.name] == engine.ledgers[table.name].live


def test_nullable_foreign_keys_stay_referentially_valid():
    # A NULL FK is referentially valid by definition; the replay's parent_refs asserts that
    # NULLs only ever appear on columns declared nullable.
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True), _col("LABEL", "varchar(10)")]),
            Table(
                "SCOTT.C",
                [
                    _col("ID", pk=True),
                    _col("P_ID", fk="SCOTT.P.ID", nullable=True, null_rate=0.4),
                    _col("NOTE", "varchar(10)"),
                ],
            ),
        ]
    ).validate()
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=5,
            initial_rows={"SCOTT.P": 10, "SCOTT.C": 40},
            ops=800,
            mix={"insert": 0.4, "update": 0.4, "delete": 0.2},
        ),
    )
    replay(schema, ops)
    assert any(op.image.get("P_ID") is None for op in ops if op.table == "SCOTT.C")


def test_updates_work_when_the_only_mutable_column_is_a_foreign_key():
    # SCOTT.C has no plain non-PK column, so the only legal change an update can make is to
    # re-point its FK at a DIFFERENT live parent -- which must still leave it referentially
    # valid and must still count as a change (R9 + R4 at once).
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True), _col("LABEL", "varchar(10)")]),
            Table("SCOTT.C", [_col("ID", pk=True), _col("P_ID", fk="SCOTT.P.ID")]),
        ]
    ).validate()
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=31,
            initial_rows={"SCOTT.P": 6, "SCOTT.C": 12},
            ops=400,
            mix={"update": 1.0},
            table_weights={"SCOTT.P": 0, "SCOTT.C": 1},
        ),
    )
    replay(schema, ops)
    updates = [op for op in ops if op.kind == "update" and op.table == "SCOTT.C"]
    assert updates, "no SCOTT.C updates were generated"
    assert all(op.before["P_ID"] != op.after["P_ID"] for op in updates)
    assert engine.fallbacks.get("nothing_to_mutate_update_to_insert", 0) == 0


def test_a_table_with_nothing_mutable_falls_back_to_insert():
    # One live parent means the child's only column cannot change; the ladder must notice
    # and insert instead of emitting a no-op update.
    schema = Schema(
        [
            Table("SCOTT.P", [_col("ID", pk=True), _col("LABEL", "varchar(10)")]),
            Table("SCOTT.C", [_col("ID", pk=True), _col("P_ID", fk="SCOTT.P.ID")]),
        ]
    ).validate()
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=33,
            initial_rows={"SCOTT.P": 1, "SCOTT.C": 3},
            ops=30,
            mix={"update": 1.0},
            table_weights={"SCOTT.P": 0, "SCOTT.C": 1},
        ),
    )
    replay(schema, ops)
    assert engine.fallbacks.get("nothing_to_mutate_update_to_insert", 0) == 30
    assert all(op.kind == "insert" for op in ops[4:])


def test_all_three_op_kinds_are_generated():
    schema = three_table_schema()
    _engine, ops = run(
        schema,
        WorkloadSpec(
            seed=1,
            initial_rows={"SCOTT.CUSTOMERS": 10},
            ops=600,
            mix={"insert": 0.4, "update": 0.4, "delete": 0.2},
        ),
    )
    assert {op.kind for op in ops} == set(KINDS)  # R6


# --- controls: counts, mix, fallbacks ------------------------------------------------------


def test_initial_rows_are_honoured_per_table():
    # R11. Phase A is the stream's prefix: topo order, insert-only, exactly the requested
    # counts -- so the first N ops are checkable directly.
    schema = three_table_schema()
    initial = {"SCOTT.CUSTOMERS": 7, "SCOTT.ORDERS": 11, "SCOTT.ORDER_ITEMS": 3}
    _engine, ops = run(schema, WorkloadSpec(seed=2, initial_rows=initial, ops=0))
    assert len(ops) == sum(initial.values())
    assert all(op.kind == "insert" for op in ops)
    counts = {}
    for op in ops:
        counts[op.table] = counts.get(op.table, 0) + 1
    assert counts == initial
    # Parents first, so every child's FK sampling had candidates (R5).
    order = [op.table for op in ops]
    assert order == (
        ["SCOTT.CUSTOMERS"] * 7 + ["SCOTT.ORDERS"] * 11 + ["SCOTT.ORDER_ITEMS"] * 3
    )


def test_a_table_absent_from_initial_rows_gets_no_initial_load():
    schema = three_table_schema()
    _engine, ops = run(
        schema, WorkloadSpec(seed=2, initial_rows={"SCOTT.CUSTOMERS": 4}, ops=0)
    )
    assert [op.table for op in ops] == ["SCOTT.CUSTOMERS"] * 4


def test_mix_ratios_are_honoured_and_fallbacks_are_accounted_for():
    # R12. Single table with no children: a delete can never be blocked by a referencing
    # child, so with a well-stocked live set the observed mix should sit on the configured
    # one, and whatever drift remains must be explained by the counted fallbacks.
    schema = single_table_schema()
    mix = {"insert": 0.5, "update": 0.3, "delete": 0.2}
    n_ops, initial = 6000, 200
    engine, ops = run(
        schema,
        WorkloadSpec(seed=8, initial_rows={"SCOTT.T": initial}, ops=n_ops, mix=mix),
    )
    phase_b = ops[initial:]
    assert len(phase_b) == n_ops
    observed = {k: sum(1 for op in phase_b if op.kind == k) for k in KINDS}
    total_fallbacks = sum(engine.fallbacks.values())
    for kind, share in mix.items():
        drift = abs(observed[kind] - share * n_ops)
        assert drift <= total_fallbacks + 0.03 * n_ops, (
            f"{kind}: observed {observed[kind]}, want ~{share * n_ops:.0f}, "
            f"fallbacks {engine.fallbacks}"
        )


def test_an_insert_only_mix_is_the_default():
    schema = single_table_schema()
    _engine, ops = run(schema, WorkloadSpec(seed=4, ops=50))
    assert {op.kind for op in ops} == {"insert"}


def test_update_on_an_empty_table_falls_back_to_insert_and_is_counted():
    schema = single_table_schema()
    engine, ops = run(schema, WorkloadSpec(seed=4, ops=20, mix={"update": 1.0}))
    assert ops[0].kind == "insert"  # nothing live yet
    assert engine.fallbacks.get("empty_table_to_insert") == 1
    assert sum(1 for op in ops if op.kind == "update") == 19


def test_delete_on_an_empty_table_falls_back_to_insert():
    # A delete-only mix on one table can only ever alternate: the table starts empty so the
    # first pick falls back to an insert, the next delete empties it again, and so on. The
    # stream stays legal at every step and each fallback is counted.
    schema = single_table_schema()
    engine, ops = run(schema, WorkloadSpec(seed=4, ops=20, mix={"delete": 1.0}))
    assert [op.kind for op in ops] == ["insert", "delete"] * 10
    assert engine.fallbacks.get("empty_table_to_insert") == 10
    replay(schema, ops)


def test_delete_of_a_fully_referenced_table_falls_back_to_update():
    # Every CUSTOMER has an ORDER pointing at it, so no customer is deletable; the ladder
    # must degrade to an update rather than orphaning a child (D2: restrict, not cascade).
    schema = three_table_schema()
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=6,
            initial_rows={"SCOTT.CUSTOMERS": 3, "SCOTT.ORDERS": 40},
            ops=200,
            mix={"delete": 1.0},
            table_weights={
                "SCOTT.CUSTOMERS": 1,
                "SCOTT.ORDERS": 0,
                "SCOTT.ORDER_ITEMS": 0,
            },
        ),
    )
    replay(schema, ops)
    assert engine.fallbacks.get("all_referenced_delete_to_update", 0) > 0


def test_child_insert_with_an_empty_parent_inserts_the_parent_instead():
    # ORDER_ITEMS needs an ORDER which needs a CUSTOMER: with nothing loaded, the ladder
    # walks all the way up the FK graph before it can insert the requested child (R5).
    schema = three_table_schema()
    engine, ops = run(
        schema,
        WorkloadSpec(
            seed=1,
            ops=6,
            mix={"insert": 1.0},
            table_weights={
                "SCOTT.CUSTOMERS": 0,
                "SCOTT.ORDERS": 0,
                "SCOTT.ORDER_ITEMS": 1,
            },
        ),
    )
    assert ops[0].table == "SCOTT.CUSTOMERS"
    assert ops[1].table == "SCOTT.ORDERS"
    assert ops[2].table == "SCOTT.ORDER_ITEMS"
    assert engine.fallbacks.get("missing_parent_insert_redirected", 0) >= 2
    replay(schema, ops)


def test_table_weights_bias_the_table_choice():
    schema = three_table_schema()
    _engine, ops = run(
        schema,
        WorkloadSpec(
            seed=7,
            initial_rows={
                "SCOTT.CUSTOMERS": 5,
                "SCOTT.ORDERS": 5,
                "SCOTT.ORDER_ITEMS": 5,
            },
            ops=3000,
            mix={"insert": 1.0},
            table_weights={
                "SCOTT.CUSTOMERS": 1,
                "SCOTT.ORDERS": 4,
                "SCOTT.ORDER_ITEMS": 0,
            },
        ),
    )
    phase_b = ops[15:]
    picked = {
        t: sum(1 for op in phase_b if op.table == t)
        for t in ("SCOTT.CUSTOMERS", "SCOTT.ORDERS", "SCOTT.ORDER_ITEMS")
    }
    assert picked["SCOTT.ORDER_ITEMS"] == 0  # weight 0 is never chosen
    assert 3.0 < picked["SCOTT.ORDERS"] / picked["SCOTT.CUSTOMERS"] < 5.0


# --- determinism ------------------------------------------------------------------------------


def _fingerprint(ops):
    return [
        (op.seq, op.txn_id, op.txn_part, op.table, op.kind, op.before, op.after)
        for op in ops
    ]


@pytest.mark.parametrize("keys", ["sequential", "uuid", "random"])
def test_same_seed_yields_an_identical_op_stream(keys):
    schema = three_table_schema()
    spec = dict(
        initial_rows={"SCOTT.CUSTOMERS": 8, "SCOTT.ORDERS": 8},
        ops=400,
        mix={"insert": 0.4, "update": 0.4, "delete": 0.2},
        txn_ops=(1, 3),
        keys=keys,
    )
    _a, ops_a = run(schema, WorkloadSpec(seed=99, **spec))
    _b, ops_b = run(three_table_schema(), WorkloadSpec(seed=99, **spec))
    assert _fingerprint(ops_a) == _fingerprint(ops_b)


def test_different_seeds_yield_different_op_streams():
    schema = three_table_schema()
    spec = dict(
        initial_rows={"SCOTT.CUSTOMERS": 8, "SCOTT.ORDERS": 8},
        ops=400,
        mix={"insert": 0.4, "update": 0.4, "delete": 0.2},
        txn_ops=(1, 3),
    )
    _a, ops_a = run(schema, WorkloadSpec(seed=99, **spec))
    _b, ops_b = run(three_table_schema(), WorkloadSpec(seed=100, **spec))
    assert _fingerprint(ops_a) != _fingerprint(ops_b)


def test_per_table_key_strategy_override():
    # R13: `keys: {SCOTT.ORDERS: uuid}` -- unlisted tables keep the sequential default.
    schema = three_table_schema()
    _engine, ops = run(
        schema,
        WorkloadSpec(
            seed=2,
            initial_rows={"SCOTT.CUSTOMERS": 5, "SCOTT.ORDERS": 5},
            ops=0,
            keys={"SCOTT.ORDERS": "uuid"},
        ),
    )
    customers = [op.after["ID"] for op in ops if op.table == "SCOTT.CUSTOMERS"]
    orders = [op.after["ID"] for op in ops if op.table == "SCOTT.ORDERS"]
    assert customers == [1, 2, 3, 4, 5]
    assert all(isinstance(o, str) and len(o) == 36 for o in orders)


# --- transactions --------------------------------------------------------------------------------


@pytest.mark.parametrize("txn_ops", [(1, 1), (1, 3), (2, 2), (3, 6)])
def test_transaction_grouping_is_well_formed(txn_ops):
    schema = three_table_schema()
    _engine, ops = run(
        schema,
        WorkloadSpec(
            seed=12,
            initial_rows={"SCOTT.CUSTOMERS": 10, "SCOTT.ORDERS": 10},
            ops=900,
            mix={"insert": 0.5, "update": 0.3, "delete": 0.2},
            txn_ops=txn_ops,
        ),
    )

    assert [op.seq for op in ops] == list(range(1, len(ops) + 1))

    groups = []
    for op in ops:
        if not groups or groups[-1][-1].txn_part in ("SOLE", "END"):
            groups.append([op])
        else:
            groups[-1].append(op)

    seen_txn_ids = set()
    for i, group in enumerate(groups, start=1):
        parts = [op.txn_part for op in group]
        ids = {op.txn_id for op in group}
        assert len(ids) == 1, f"transaction {i} spans txn ids {ids}"
        assert ids.isdisjoint(seen_txn_ids), f"txn id {ids} reused"
        seen_txn_ids |= ids
        assert group[0].txn_id == i, "txn ids increment by one per transaction"
        if len(group) == 1:
            assert parts == ["SOLE"]
        else:
            assert parts[0] == "BEGIN" and parts[-1] == "END"
            assert all(p == "MIDDLE" for p in parts[1:-1])
        assert txn_ops[0] <= len(group) <= txn_ops[1] or group is groups[-1]


def test_single_op_transactions_are_sole():
    schema = single_table_schema()
    _engine, ops = run(schema, WorkloadSpec(seed=3, ops=25, txn_ops=(1, 1)))
    assert all(op.txn_part == "SOLE" for op in ops)
    assert [op.txn_id for op in ops] == list(range(1, 26))


def test_transactions_may_span_tables():
    schema = three_table_schema()
    _engine, ops = run(
        schema,
        WorkloadSpec(
            seed=14,
            initial_rows={
                "SCOTT.CUSTOMERS": 5,
                "SCOTT.ORDERS": 5,
                "SCOTT.ORDER_ITEMS": 5,
            },
            ops=600,
            mix={"insert": 1.0},
            txn_ops=(3, 3),
        ),
    )
    by_txn: dict[int, set] = {}
    for op in ops:
        by_txn.setdefault(op.txn_id, set()).add(op.table)
    assert any(len(tables) > 1 for tables in by_txn.values())


# --- configuration errors ---------------------------------------------------------------------------


def test_unknown_table_in_initial_rows_is_rejected():
    with pytest.raises(WorkloadError, match="unknown table"):
        WorkloadEngine(
            single_table_schema(), WorkloadSpec(initial_rows={"SCOTT.NOPE": 1})
        )


def test_unknown_table_in_table_weights_is_rejected():
    with pytest.raises(WorkloadError, match="unknown table"):
        WorkloadEngine(
            single_table_schema(), WorkloadSpec(table_weights={"SCOTT.NOPE": 1})
        )


def test_unknown_op_in_mix_is_rejected():
    with pytest.raises(WorkloadError, match="unknown op"):
        WorkloadEngine(single_table_schema(), WorkloadSpec(ops=1, mix={"upsert": 1.0}))


def test_an_all_zero_mix_is_rejected():
    with pytest.raises(WorkloadError, match="no positive weight"):
        WorkloadEngine(single_table_schema(), WorkloadSpec(ops=1, mix={"insert": 0.0}))


@pytest.mark.parametrize("txn_ops", [(0, 3), (4, 2), (-1, 1)])
def test_malformed_txn_ops_are_rejected(txn_ops):
    with pytest.raises(WorkloadError, match="txn_ops"):
        WorkloadEngine(single_table_schema(), WorkloadSpec(ops=1, txn_ops=txn_ops))


# --- R14 volume smoke ----------------------------------------------------------------------------------


def test_a_hundred_thousand_ops_stream_with_a_bounded_ledger():
    # R14: the op stream is never materialised, so the only memory that grows is the LIVE
    # set. A balanced mix keeps that bounded while the op count runs far past it -- this
    # asserts the ledger stays small AND that per-op cost has not gone superlinear.
    schema = three_table_schema()
    engine = WorkloadEngine(
        schema,
        WorkloadSpec(
            seed=21,
            initial_rows={
                "SCOTT.CUSTOMERS": 200,
                "SCOTT.ORDERS": 400,
                "SCOTT.ORDER_ITEMS": 400,
            },
            ops=100_000,
            mix={"insert": 0.34, "update": 0.33, "delete": 0.33},
            txn_ops=(1, 4),
        ),
    )

    started = time.monotonic()
    count = 0
    peak_live = 0
    for _op in engine.ops():  # consumed lazily: nothing accumulates
        count += 1
        if count % 10_000 == 0:
            peak_live = max(peak_live, sum(engine.live_counts().values()))
    elapsed = time.monotonic() - started

    assert count == 101_000
    assert elapsed < 60, f"100k ops took {elapsed:.1f}s"
    assert (
        peak_live < 60_000
    ), f"live set grew to {peak_live}; the ledger is not bounded"
    assert sum(engine.counts.values()) == count
