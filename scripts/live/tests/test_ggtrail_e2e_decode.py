from __future__ import annotations
import os

import pytest

from livetest.ggtrail.model import parse_dtype
from livetest.ggtrail.runner import generate
from livetest.ggtrail.workload import WorkloadEngine
from livetest.ggtrail.yamlio import load_workload

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle"
)
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]

# Tier 2 (spec §10) -- the generated multi-table workload decoded by Striim's REAL
# GGTrailParser, then compared op-for-op against the engine stream that produced it.
# GGTrailParser has no serializer, so this decode oracle is the only way to prove the
# Layer 1 -> Layer 0 handoff actually lands on the wire the way we think it does:
# R1 (several related tables in one trail), R6 (I/U/D arrive as INSERT/UPDATE/DELETE) and
# R10 on the wire (values survive encoding, including SINT64 scaling and NULLs).

WORKLOAD_YAML = """\
seed: 4242
keys: sequential
tables:
  SCOTT.CUSTOMERS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: NAME, type: "varchar(50)"}
      - {name: TIER, type: "char(1)"}
  SCOTT.ORDERS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: CUSTOMER_ID, type: int, fk: SCOTT.CUSTOMERS.ID}
      - {name: AMOUNT, type: "decimal(10,2)"}
      - {name: NOTE, type: "varchar(20)", nullable: true, null_rate: 0.3}
  SCOTT.ORDER_ITEMS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: ORDER_ID, type: int, fk: SCOTT.ORDERS.ID}
      - {name: SKU, type: "varchar(20)"}
      - {name: QTY, type: int}
workload:
  initial_rows: {SCOTT.CUSTOMERS: 6, SCOTT.ORDERS: 10, SCOTT.ORDER_ITEMS: 12}
  ops: 60
  mix: {insert: 0.5, update: 0.3, delete: 0.2}
  table_weights: {SCOTT.CUSTOMERS: 1, SCOTT.ORDERS: 2, SCOTT.ORDER_ITEMS: 2}
  txn_ops: [1, 3]
max_records_per_file: 1000
"""

_OP_NAMES = {"insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}


@pytest.fixture(scope="module")
def decoded(tmp_path_factory):
    """Generate the workload, decode the whole directory, return (ops, records)."""
    from livetest.ggtrail import harness

    tmp_path = tmp_path_factory.mktemp("ggtrail-e2e")
    workload = tmp_path / "workload.yaml"
    workload.write_text(WORKLOAD_YAML)
    out = tmp_path / "out"

    schema, spec, extras = load_workload(workload)
    result = generate(
        schema,
        spec,
        out,
        max_records_per_file=extras.get("max_records_per_file") or 1000,
    )
    ops = list(WorkloadEngine(schema, spec).ops())  # the same stream, replayed
    records = harness.decode_dir(out, result["def_file"])
    return schema, ops, records


def _same_value(dtype: str, expected, decoded_text) -> bool:
    # Every decoded value arrives as a string (TrailDumpHarness stringifies whatever
    # GGTrailParser produced), so numerics are compared NUMERICALLY -- a SINT64 that
    # round-trips correctly may still render as "2.2" where we wrote "2.20".
    if expected is None:
        return decoded_text is None
    if decoded_text is None:
        return False
    base, _args = parse_dtype(dtype)
    if base in ("int", "decimal"):
        return abs(float(decoded_text) - float(expected)) < 1e-9
    if base == "char":
        # ASCII_F is space padded; whether the parser hands the padding back is its call.
        return decoded_text.rstrip() == str(expected).rstrip()
    return decoded_text == str(expected)


def test_every_op_decodes_in_order_with_the_right_table_and_type(decoded):
    _schema, ops, records = decoded
    assert len(records) == len(ops), "decoded record count != generated op count"
    for op, record in zip(ops, records):
        assert (
            record["table"].upper() == op.table.upper()
        ), f"op {op.seq}: table mismatch"
        assert record["op_type"] == _OP_NAMES[op.kind], f"op {op.seq}: op type mismatch"


def test_all_three_tables_appear_in_the_one_trail(decoded):
    # R1: several related tables carried by a single trail directory + one schema.def.
    schema, _ops, records = decoded
    assert {r["table"].upper() for r in records} == {t.name for t in schema.tables}


def test_all_three_operation_kinds_decode(decoded):
    # R6: inserts, updates and deletes arrive AS inserts, updates and deletes.
    _schema, _ops, records = decoded
    assert {r["op_type"] for r in records} == {"INSERT", "UPDATE", "DELETE"}


def test_every_column_value_survives_the_round_trip(decoded):
    # R10 on the wire, including SINT64 scaling (decimal(10,2)) and int columns.
    schema, ops, records = decoded
    for op, record in zip(ops, records):
        table = schema.table(op.table)
        image = op.image
        for column in table.columns:
            got = record["columns"].get(column.name)
            assert _same_value(column.dtype, image.get(column.name), got), (
                f"op {op.seq} {op.table}.{column.name}: "
                f"wrote {image.get(column.name)!r}, decoded {got!r}"
            )


def test_decimal_columns_keep_their_scale(decoded):
    # The SINT64 span carries an integer scaled by 10**scale; a scaling bug shows up as a
    # value off by a power of ten, which the numeric compare above would catch but this
    # names explicitly.
    schema, ops, records = decoded
    checked = 0
    for op, record in zip(ops, records):
        if op.table != "SCOTT.ORDERS":
            continue
        wrote = float(op.image["AMOUNT"])
        assert abs(float(record["columns"]["AMOUNT"]) - wrote) < 1e-9
        checked += 1
    assert checked, "no SCOTT.ORDERS ops in the stream to check"


def test_nulls_decode_as_nulls(decoded):
    schema, ops, records = decoded
    nulls = 0
    for op, record in zip(ops, records):
        if op.table != "SCOTT.ORDERS":
            continue
        if op.image.get("NOTE") is None:
            assert (
                record["columns"]["NOTE"] is None
            ), f"op {op.seq}: NULL decoded as a value"
            nulls += 1
        else:
            assert record["columns"]["NOTE"] is not None
    assert nulls, "the workload produced no NULLs to check"


def test_transaction_grouping_survives_the_round_trip(decoded):
    """Records in one engine transaction share one decoded txn id, and consecutive
    transactions get distinct ones (spec §12.3's BEGIN/MIDDLE/END framing)."""
    _schema, ops, records = decoded

    engine_groups: list[list[int]] = []
    for i, op in enumerate(ops):
        if not engine_groups or ops[i - 1].txn_part in ("SOLE", "END"):
            engine_groups.append([i])
        else:
            engine_groups[-1].append(i)

    seen = set()
    for group in engine_groups:
        ids = {records[i]["txn_id"] for i in group}
        assert (
            len(ids) == 1
        ), f"ops {group} were one transaction but decoded as txn ids {ids}"
        txn_id = ids.pop()
        assert txn_id not in seen, f"decoded txn id {txn_id} reused across transactions"
        seen.add(txn_id)

    assert any(
        len(g) > 1 for g in engine_groups
    ), "workload produced no multi-op transactions"
