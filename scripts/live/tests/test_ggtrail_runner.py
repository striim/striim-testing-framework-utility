from __future__ import annotations
import csv
import json
import shutil
import textwrap
from pathlib import Path

import pytest

from livetest.assertions.file import assert_file
from livetest.ggtrail.model import Column, Schema, Table
from livetest.ggtrail.runner import (
    BASE_TIME,
    generate,
    generate_from_yaml,
    projection_width,
)
from livetest.ggtrail.workload import WorkloadEngine, WorkloadSpec
from livetest.ggtrail.yamlio import load_workload

# Tier 1 (spec §10) -- hermetic runner tests: no Java, no Docker, no Striim. They prove the
# runner faithfully TRANSCRIBES the engine stream (whose semantics test_ggtrail_workload
# owns) into the artifacts plugin.py places and the assertions consume.

WORKLOAD_YAML = """\
seed: 42
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
      - {name: PLACED_AT, type: timestamp}
  SCOTT.ORDER_ITEMS:
    columns:
      - {name: ID, type: int, pk: true}
      - {name: ORDER_ID, type: int, fk: SCOTT.ORDERS.ID}
      - {name: SKU, type: "varchar(20)"}
      - {name: QTY, type: int}
workload:
  initial_rows: {SCOTT.CUSTOMERS: 8, SCOTT.ORDERS: 12, SCOTT.ORDER_ITEMS: 15}
  ops: 60
  mix: {insert: 0.6, update: 0.3, delete: 0.1}
  table_weights: {SCOTT.CUSTOMERS: 1, SCOTT.ORDERS: 2, SCOTT.ORDER_ITEMS: 2}
  txn_ops: [1, 3]
max_records_per_file: 1000
"""


@pytest.fixture
def workload_file(tmp_path) -> Path:
    path = tmp_path / "workload.yaml"
    path.write_text(WORKLOAD_YAML)
    return path


def replay_ops(workload: Path):
    """The reference op stream: a second engine on the same (schema, spec)."""
    schema, spec, _extras = load_workload(workload)
    return schema, spec, list(WorkloadEngine(schema, spec).ops())


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


# --- artifacts ---------------------------------------------------------------------------


def test_generate_from_yaml_returns_the_plugin_contract(workload_file, tmp_path):
    out = tmp_path / "out"
    result = generate_from_yaml(workload_file, out)

    assert set(result) == {"trail_files", "def_file", "expected_dir", "summary"}
    assert result["trail_files"] and all(p.exists() for p in result["trail_files"])
    assert all(p.name.startswith("rt") for p in result["trail_files"])
    assert result["def_file"] == out / "schema.def" and result["def_file"].exists()
    assert result["expected_dir"] == out / "expected"
    assert (out / "summary.json").exists()


def test_the_def_file_declares_every_table(workload_file, tmp_path):
    generate_from_yaml(workload_file, tmp_path / "out")
    text = (tmp_path / "out" / "schema.def").read_text()
    for table in ("SCOTT.CUSTOMERS", "SCOTT.ORDERS", "SCOTT.ORDER_ITEMS"):
        assert f"Definition for table {table}" in text
    assert "Defgen version 2.0" in text


def test_trail_files_roll_over_at_max_records_per_file(tmp_path):
    schema, spec, _extras = load_workload(_write(tmp_path, WORKLOAD_YAML))
    out = tmp_path / "out"
    result = generate(schema, spec, out, max_records_per_file=25)
    records = result["summary"]["records"]

    # Rollover is checked BEFORE each write, so N records fill ceil(N/25) files.
    assert len(result["trail_files"]) == -(-records // 25)
    assert [p.name for p in result["trail_files"]] == sorted(
        p.name for p in result["trail_files"]
    )
    assert all(p.stat().st_size > 0 for p in result["trail_files"])


def test_a_single_file_holds_everything_when_the_rollover_is_large(
    workload_file, tmp_path
):
    result = generate_from_yaml(workload_file, tmp_path / "out")
    assert [p.name for p in result["trail_files"]] == ["rt0000000"]


# --- expected/ops.csv -------------------------------------------------------------------------


def test_ops_csv_header_matches_the_app_tql_projection(workload_file, tmp_path):
    # app.tql's CQ names its fields TABLE_NAME/OP_TYPE/C0/C1/C2, and the file: assertion
    # compares those names against the golden's csv.DictReader keys.
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    with open(out / "expected" / "ops.csv", newline="") as fh:
        assert next(csv.reader(fh)) == ["TABLE_NAME", "OP_TYPE", "C0", "C1", "C2"]


def test_ops_csv_matches_an_independent_replay_of_the_engine(workload_file, tmp_path):
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    schema, _spec, ops = replay_ops(workload_file)

    want = []
    for op in ops:
        table = schema.table(op.table)
        image = op.image
        want.append(
            {
                "TABLE_NAME": op.table,
                "OP_TYPE": {"insert": "INSERT", "update": "UPDATE", "delete": "DELETE"}[
                    op.kind
                ],
                **{
                    f"C{i}": (
                        "" if image.get(c.name) is None else str(image.get(c.name))
                    )
                    for i, c in enumerate(table.columns[:3])
                },
            }
        )
    assert read_csv(out / "expected" / "ops.csv") == want


def test_deletes_project_their_before_image_and_updates_their_after(
    workload_file, tmp_path
):
    # Follows what Layer 0 puts on the wire: write_delete encodes the pre-image,
    # write_update the after-image -- so the golden must project the same one.
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    _schema, _spec, ops = replay_ops(workload_file)
    rows = read_csv(out / "expected" / "ops.csv")

    for op, row in zip(ops, rows):
        if op.kind == "delete":
            assert row["C0"] == str(op.before["ID"])
        elif op.kind == "update":
            assert row["C0"] == str(op.after["ID"])
    assert any(op.kind == "delete" for op in ops) and any(
        op.kind == "update" for op in ops
    )


def test_ops_csv_is_consumable_by_the_real_file_assertion(workload_file, tmp_path):
    """The contract that matters: feed the assertion the JSON events a Striim FileWriter
    would produce for this op stream and prove it matches the generated golden."""
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    rows = read_csv(out / "expected" / "ops.csv")
    events = json.dumps([{"data": dict(row)} for row in rows])

    spec = {"path": "/tmp/fake-out", "match": "expected/ops.csv", "project": "data"}
    results = assert_file(
        lambda _path: events, [spec], basedir=out, timeout=1, poll=0.01
    )
    assert [r["status"] for r in results] == ["passed"]


def test_the_file_assertion_notices_a_wrong_event_stream(workload_file, tmp_path):
    from livetest.assertions import AssertionFailed

    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    rows = read_csv(out / "expected" / "ops.csv")
    rows[0]["OP_TYPE"] = "TRUNCATE"  # one corrupted event
    events = json.dumps([{"data": dict(row)} for row in rows])

    spec = {"path": "/tmp/fake-out", "match": "expected/ops.csv", "project": "data"}
    with pytest.raises(AssertionFailed):
        assert_file(lambda _path: events, [spec], basedir=out, timeout=0, poll=0.01)


def test_projection_width_is_the_widest_common_prefix():
    narrow = Schema(
        [
            Table("SCOTT.A", [Column("ID", "int", pk=True), Column("N", "varchar(4)")]),
            Table(
                "SCOTT.B",
                [
                    Column("ID", "int", pk=True),
                    Column("N", "varchar(4)"),
                    Column("M", "varchar(4)"),
                ],
            ),
        ]
    ).validate()
    assert projection_width(narrow) == 2

    wide = Schema(
        [
            Table(
                "SCOTT.A",
                [
                    Column("ID", "int", pk=True),
                    Column("B", "int"),
                    Column("C", "int"),
                    Column("D", "int"),
                ],
            )
        ]
    ).validate()
    assert projection_width(wide) == 3  # capped at what app.tql reads


def test_a_narrow_schema_emits_a_narrower_golden(tmp_path):
    schema = Schema(
        [
            Table("SCOTT.A", [Column("ID", "int", pk=True), Column("N", "varchar(4)")]),
        ]
    ).validate()
    out = tmp_path / "out"
    result = generate(schema, WorkloadSpec(seed=1, initial_rows={"SCOTT.A": 5}), out)
    with open(out / "expected" / "ops.csv", newline="") as fh:
        assert next(csv.reader(fh)) == ["TABLE_NAME", "OP_TYPE", "C0", "C1"]
    assert result["summary"]["projection_width"] == 2


# --- expected/ops_detail.csv + expected/state ------------------------------------------------------


def test_ops_detail_carries_the_per_op_trace(workload_file, tmp_path):
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    _schema, _spec, ops = replay_ops(workload_file)
    rows = read_csv(out / "expected" / "ops_detail.csv")

    assert len(rows) == len(ops)
    assert [int(r["seq"]) for r in rows] == [op.seq for op in ops]
    assert [int(r["txn_id"]) for r in rows] == [op.txn_id for op in ops]
    assert [r["txn_part"] for r in rows] == [op.txn_part for op in ops]
    for row, op in zip(rows, ops):
        if op.kind == "update":
            changed = row["changed_cols"].split()
            assert changed and all(op.before[c] != op.after[c] for c in changed)
        elif op.kind == "delete":
            assert row["changed_cols"] == ""


def test_state_csvs_match_the_final_live_rows(workload_file, tmp_path):
    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    schema, spec, _ops = replay_ops(workload_file)

    engine = WorkloadEngine(schema, spec)
    for _op in engine.ops():
        pass

    for table in schema.tables:
        rows = read_csv(out / "expected" / "state" / f"{table.name}.csv")
        live = engine.ledgers[table.name].live
        assert len(rows) == len(live)
        want = {
            tuple(
                "" if r.get(c.name) is None else str(r.get(c.name))
                for c in table.columns
            )
            for r in live.values()
        }
        got = {tuple(row[c.name] for c in table.columns) for row in rows}
        assert got == want


def test_state_csvs_are_sorted_by_primary_key(tmp_path):
    schema = Schema(
        [Table("SCOTT.A", [Column("ID", "int", pk=True), Column("N", "varchar(6)")])]
    ).validate()
    out = tmp_path / "out"
    generate(
        schema,
        WorkloadSpec(
            seed=3,
            initial_rows={"SCOTT.A": 30},
            ops=40,
            mix={"insert": 0.5, "delete": 0.5},
        ),
        out,
    )
    ids = [row["ID"] for row in read_csv(out / "expected" / "state" / "SCOTT.A.csv")]
    assert ids == sorted(ids, key=repr)


# --- summary.json ----------------------------------------------------------------------------------


def test_summary_counts_are_correct(workload_file, tmp_path):
    out = tmp_path / "out"
    result = generate_from_yaml(workload_file, out)
    summary = result["summary"]
    _schema, _spec, ops = replay_ops(workload_file)

    assert summary["seed"] == 42
    assert summary["records"] == len(ops)
    assert summary["transactions"] == ops[-1].txn_id
    assert summary["by_op"] == {
        k: sum(1 for op in ops if op.kind == k) for k in ("insert", "update", "delete")
    }
    for table, counts in summary["by_table"].items():
        for kind, n in counts.items():
            assert n == sum(1 for op in ops if op.table == table and op.kind == kind)
    assert sum(summary["by_op"].values()) == summary["records"]
    assert summary["trail_files"] == [p.name for p in result["trail_files"]]
    assert summary["config"]["ops"] == 60
    assert (
        json.loads((out / "summary.json").read_text())["records"] == summary["records"]
    )


def test_summary_reports_fallbacks(tmp_path):
    schema = Schema(
        [Table("SCOTT.A", [Column("ID", "int", pk=True), Column("N", "varchar(6)")])]
    ).validate()
    out = tmp_path / "out"
    result = generate(schema, WorkloadSpec(seed=1, ops=10, mix={"update": 1.0}), out)
    # A skewed config must be visible, never silent (spec §7.2).
    assert result["summary"]["fallbacks"]["empty_table_to_insert"] == 1


def test_final_live_rows_match_the_state_csvs(workload_file, tmp_path):
    out = tmp_path / "out"
    result = generate_from_yaml(workload_file, out)
    for table, count in result["summary"]["final_live_rows"].items():
        assert len(read_csv(out / "expected" / "state" / f"{table}.csv")) == count


# --- determinism ---------------------------------------------------------------------------------------


def test_two_runs_of_the_same_seed_produce_identical_trail_bytes(
    workload_file, tmp_path
):
    # Same PATH both times on purpose: the trail header embeds `uri:<directory>` (Layer 0
    # header.py), so a different output directory legitimately changes the bytes.
    out = tmp_path / "out"

    generate_from_yaml(workload_file, out)
    first = {p.name: p.read_bytes() for p in sorted(out.glob("rt*"))}
    first_def = (out / "schema.def").read_bytes()
    first_ops = (out / "expected" / "ops.csv").read_bytes()

    shutil.rmtree(out)
    generate_from_yaml(workload_file, out)
    second = {p.name: p.read_bytes() for p in sorted(out.glob("rt*"))}

    assert first and first == second
    assert (out / "schema.def").read_bytes() == first_def
    assert (out / "expected" / "ops.csv").read_bytes() == first_ops


def test_record_timestamps_come_from_the_deterministic_clock(workload_file, tmp_path):
    # Batch mode must never read the wall clock -- the timestamp is part of the bytes that
    # byte-determinism above depends on. BASE_TIME's Julian encoding appears verbatim.
    from livetest.ggtrail import tlv

    out = tmp_path / "out"
    generate_from_yaml(workload_file, out)
    blob = (out / "rt0000000").read_bytes()
    assert tlv.tjulian(BASE_TIME) in blob


def test_a_different_seed_changes_the_trail(tmp_path, workload_file):
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    schema, spec, _extras = load_workload(workload_file)
    generate(schema, spec, out_a)
    schema_b, spec_b, _ = load_workload(workload_file)
    spec_b.seed = 4242
    generate(schema_b, spec_b, out_b)
    assert (out_a / "expected" / "ops.csv").read_text() != (
        out_b / "expected" / "ops.csv"
    ).read_text()


# --- rate mode -------------------------------------------------------------------------------------------


def test_stream_mode_emits_the_whole_workload_when_unbounded(workload_file, tmp_path):
    from livetest.ggtrail.runner import stream_from_yaml

    _schema, _spec, ops = replay_ops(workload_file)
    out = tmp_path / "out"
    result = stream_from_yaml(workload_file, out, rate=20_000)
    assert result["summary"]["records"] == len(ops)
    assert result["trail_files"] and result["def_file"].exists()


def test_stream_mode_stops_at_its_duration(workload_file, tmp_path):
    # The emulator paces appends by wall clock, so a duration truncates the stream -- the
    # artifacts must still describe exactly what WAS written.
    from livetest.ggtrail.runner import stream_from_yaml

    _schema, _spec, ops = replay_ops(workload_file)
    out = tmp_path / "out"
    result = stream_from_yaml(workload_file, out, rate=100, duration=0.2)
    written = result["summary"]["records"]
    assert 0 < written < len(ops)
    assert len(read_csv(out / "expected" / "ops.csv")) == written


def test_stream_mode_rejects_a_non_positive_rate(workload_file, tmp_path):
    from livetest.ggtrail.runner import stream_from_yaml

    with pytest.raises(ValueError, match="positive ops/sec"):
        stream_from_yaml(workload_file, tmp_path / "out", rate=0)


# --- helpers -------------------------------------------------------------------------------------------


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "workload.yaml"
    path.write_text(textwrap.dedent(text))
    return path
