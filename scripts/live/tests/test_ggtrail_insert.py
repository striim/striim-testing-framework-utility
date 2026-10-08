from __future__ import annotations
import datetime
import os
import pytest
from pathlib import Path
from livetest.ggtrail.record import write_insert
from livetest.ggtrail.header import write_header
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V, ASCII_F, SINT64

pytestmark = pytest.mark.skipif(not os.environ.get("STRIIM_HOME"), reason="requires STRIIM_HOME for the Java oracle")
# The Java oracle is tools/ggtrail-harness in your test repo: without it, skip, even with STRIIM_HOME set.
from livetest.ggtrail import harness as _ggtrail_harness  # noqa: E402
_NO_HARNESS = _ggtrail_harness.missing_reason()
pytestmark = [pytestmark, pytest.mark.skipif(_NO_HARNESS is not None, reason=_NO_HARNESS or "")]

SCHEMA = TableSchema("SCOTT.WIDGETS", [
    ColumnSpec("ID", ASCII_V, 10),
    ColumnSpec("NAME", ASCII_V, 50),
])

# Mixed-type table exercising every wire encoding record._encode_column supports.
TYPED_SCHEMA = TableSchema("SCOTT.TYPED", [
    ColumnSpec("ID", ASCII_V, 10, is_key=True),
    ColumnSpec("CODE", ASCII_F, 4),
    ColumnSpec("AMOUNT", SINT64, 22, scale=3),
    ColumnSpec("NOTE", ASCII_V, 50),
])


def test_generated_insert_decodes_to_the_values_written(tmp_path):
    from livetest.ggtrail import harness

    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_insert(SCHEMA, {"ID": "42", "NAME": "COMPANY 1"}, txn_id=1001, timestamp=ts)
    (trail_dir / "rt0000000").write_bytes(body)

    def_file = tmp_path / "widgets.def"
    write_def_file(def_file, [SCHEMA])

    records = harness.decode_dir(trail_dir, def_file)
    assert len(records) == 1
    assert records[0]["op_type"] == "INSERT"
    assert records[0]["table"] == "SCOTT.WIDGETS"
    assert records[0]["columns"]["ID"] == "42"
    assert records[0]["columns"]["NAME"] == "COMPANY 1"


def test_sint64_and_null_columns_decode(tmp_path):
    # SINT64 (Evidence §D): span = [u16 null-ind = 0][8-byte BE int], colLen 10, decoded
    # value = int * 10^-scale -- so 2.200 at scale 3 goes out as the integer 2200.
    # NULL: span FF FF (both null-indicator bytes non-zero).
    from livetest.ggtrail import harness

    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    ts = datetime.datetime(2026, 7, 14, 12, 0, 0)
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=ts)
    body += write_insert(TYPED_SCHEMA,
                         {"ID": "1", "CODE": "0916", "AMOUNT": 2.2, "NOTE": None},
                         txn_id=1010, timestamp=ts)
    (trail_dir / "rt0000000").write_bytes(body)

    def_file = tmp_path / "typed.def"
    write_def_file(def_file, [TYPED_SCHEMA])

    records = harness.decode_dir(trail_dir, def_file)
    assert len(records) == 1
    cols = records[0]["columns"]
    assert cols["ID"] == "1"
    assert cols["CODE"] == "0916"
    assert float(cols["AMOUNT"]) == pytest.approx(2.2)
    assert cols["NOTE"] is None
