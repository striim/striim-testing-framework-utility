from __future__ import annotations
import datetime
import os
import pytest
from livetest.ggtrail.writer import TrailFileWriter
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, ASCII_V

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]

SCHEMA = TableSchema("SCOTT.WIDGETS", [ColumnSpec("ID", ASCII_V, 10), ColumnSpec("NAME", ASCII_V, 50)])
ORDERS = TableSchema("SCOTT.ORDERS", [ColumnSpec("ID", ASCII_V, 10, is_key=True),
                                      ColumnSpec("WIDGET_ID", ASCII_V, 10)])


def test_writer_rolls_over_and_all_records_decode(tmp_path):
    from livetest.ggtrail import harness

    w = TrailFileWriter(tmp_path, [SCHEMA], max_records_per_file=2)
    for i in range(5):
        w.insert("SCOTT.WIDGETS", {"ID": str(i), "NAME": f"widget-{i}"})
    w.close()

    trail_files = sorted(tmp_path.glob("rt0000*"))
    assert len(trail_files) == 3   # 2 + 2 + 1 records across 3 files

    records = harness.decode_dir(tmp_path, tmp_path / "schema.def")
    assert len(records) == 5
    assert {r["columns"]["NAME"] for r in records} == {f"widget-{i}" for i in range(5)}


def test_multi_table_writer_decodes_both_tables(tmp_path):
    # Spec §12.1: one writer, one schema.def, records interleaved across tables.
    from livetest.ggtrail import harness

    w = TrailFileWriter(tmp_path, [SCHEMA, ORDERS])
    w.insert("SCOTT.WIDGETS", {"ID": "1", "NAME": "bolt"})
    w.insert("SCOTT.ORDERS", {"ID": "9", "WIDGET_ID": "1"})
    w.update("SCOTT.WIDGETS", before={"ID": "1", "NAME": "bolt"}, after={"ID": "1", "NAME": "nut"})
    w.delete("SCOTT.ORDERS", {"ID": "9", "WIDGET_ID": "1"})
    w.close()

    records = harness.decode_dir(tmp_path, tmp_path / "schema.def")
    assert [r["table"] for r in records] == [
        "SCOTT.WIDGETS", "SCOTT.ORDERS", "SCOTT.WIDGETS", "SCOTT.ORDERS",
    ]
    assert [r["op_type"] for r in records] == ["INSERT", "INSERT", "UPDATE", "DELETE"]


def test_cross_table_transaction_shares_one_txn_id(tmp_path):
    # Spec §12.3: one transaction spanning tables -- shared txn_id, BEGIN/MIDDLE/END parts.
    from livetest.ggtrail import harness

    w = TrailFileWriter(tmp_path, [SCHEMA, ORDERS])
    w.transaction([
        ("SCOTT.WIDGETS", "insert", {"ID": "1", "NAME": "bolt"}),
        ("SCOTT.ORDERS", "insert", {"ID": "9", "WIDGET_ID": "1"}),
        ("SCOTT.ORDERS", "update", {"ID": "9", "WIDGET_ID": "1"}, {"ID": "9", "WIDGET_ID": "2"}),
    ])
    w.close()

    records = harness.decode_dir(tmp_path, tmp_path / "schema.def")
    assert len(records) == 3
    assert len({r["txn_id"] for r in records}) == 1
