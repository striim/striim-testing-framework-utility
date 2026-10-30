"""An UPDATE that changes the key is written as UPDATEPK with its before-image key, and
txn_tokens puts GoldenGate's TxnID and CSN tokens on every record."""
from __future__ import annotations
import datetime
import os
import struct
import pytest
from livetest.ggtrail import record as rec
from livetest.ggtrail.header import write_header
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, write_def_file, ASCII_V
from livetest.ggtrail.writer import TrailFileWriter
from livetest.ggtrail import harness as _ggtrail_harness

KEYED = TableSchema("SCOTT.WIDGETS", [ColumnSpec("ID", ASCII_V, 10, is_key=True),
                                      ColumnSpec("NAME", ASCII_V, 50)])
TS = datetime.datetime(2026, 7, 14, 12, 0, 0)


def _chunks(record: bytes) -> tuple[bytes, bytes | None]:
    """(data chunk columns, token chunk body or None), as RecordUtil locates them."""
    header_len = struct.unpack(">H", record[6:8])[0]
    pos = 8 + header_len
    assert record[pos] == rec._RECORD_DATA_TOKEN
    data_len = struct.unpack(">H", record[pos + 2:pos + 4])[0]
    columns = record[pos + 4:pos + 4 + data_len]
    pos += 4 + data_len
    if record[pos] != rec._RECORD_TOKENS_TOKEN:
        return columns, None
    token_len = struct.unpack(">H", record[pos + 2:pos + 4])[0]
    return columns, record[pos + 4:pos + 4 + token_len]


def _io_type(record: bytes) -> int:
    return record[8 + rec._OPERATION_TYPE_OFFSET]


def test_key_change_is_updatepk_with_the_before_key_first():
    body = rec.write_update(KEYED, before={"ID": "1", "NAME": "A"}, after={"ID": "2", "NAME": "A"},
                            txn_id=7, timestamp=TS)
    assert _io_type(body) == rec._DBOP_UPDATEPK
    columns, tokens = _chunks(body)
    before_key = rec._encode_column(0, KEYED.columns[0], "1")
    # [u16 length counting itself][before key], then the after-image as for any UPDATE
    assert columns.startswith(struct.pack(">H", 2 + len(before_key)) + before_key)
    assert columns[2 + len(before_key):] == rec._encode_columns(KEYED, {"ID": "2", "NAME": "A"})
    assert tokens is None


def test_same_key_or_no_key_column_stays_a_plain_update():
    same = rec.write_update(KEYED, before={"ID": "1", "NAME": "A"}, after={"ID": "1", "NAME": "B"},
                            txn_id=7, timestamp=TS)
    unkeyed = TableSchema("SCOTT.W", [ColumnSpec("ID", ASCII_V, 10), ColumnSpec("NAME", ASCII_V, 50)])
    moved = rec.write_update(unkeyed, before={"ID": "1"}, after={"ID": "2", "NAME": "B"},
                             txn_id=7, timestamp=TS)
    assert _io_type(same) == _io_type(moved) == rec._DBOP_UPDATE


def test_tokens_on_every_record_and_only_when_asked():
    first = rec.write_insert(KEYED, {"ID": "1", "NAME": "A"}, 9, TS, txn_part=rec._TXN_PART_BEGIN,
                             txn_tokens=True)
    middle = rec.write_insert(KEYED, {"ID": "2", "NAME": "A"}, 9, TS, txn_part=rec._TXN_PART_MIDDLE,
                              txn_tokens=True)
    sole = rec.write_delete(KEYED, {"ID": "3"}, 10, TS, txn_tokens=True)
    off = rec.write_insert(KEYED, {"ID": "4", "NAME": "A"}, 11, TS)
    value = b"9"
    item = lambda code: bytes([code, 0]) + struct.pack(">H", len(value)) + value  # noqa: E731
    assert _chunks(first)[1] == _chunks(middle)[1] == item(rec._TOKEN_TXNID) + item(rec._TOKEN_CSN)
    assert _chunks(sole)[1] is not None
    assert _chunks(off)[1] is None
    # the trailing length still equals the leading one
    for r in (first, middle, sole, off):
        assert r[2:4] == r[-2:] and len(r) == struct.unpack(">H", r[2:4])[0]


def test_writer_passes_txn_tokens_through(tmp_path):
    w = TrailFileWriter(tmp_path, [KEYED], clock=lambda: TS, txn_tokens=True)
    w.transaction([("SCOTT.WIDGETS", "update", {"ID": "1", "NAME": "A"}, {"ID": "2", "NAME": "A"}),
                   ("SCOTT.WIDGETS", "update", {"ID": "2", "NAME": "B"}, {"ID": "1", "NAME": "B"})])
    w.close()
    data = (tmp_path / "rt0000000").read_bytes()
    for i, (before, after, part) in enumerate((
            ({"ID": "1", "NAME": "A"}, {"ID": "2", "NAME": "A"}, rec._TXN_PART_BEGIN),
            ({"ID": "2", "NAME": "B"}, {"ID": "1", "NAME": "B"}, rec._TXN_PART_END))):
        r = rec.write_update(KEYED, before, after, 1, TS, txn_part=part, txn_tokens=True)
        assert r in data and _chunks(r)[1] is not None, i


_NO_ORACLE = (None if os.environ.get("STRIIM_HOME") else "requires STRIIM_HOME for the Java oracle") \
    or _ggtrail_harness.missing_reason()


@pytest.mark.skipif(_NO_ORACLE is not None, reason=_NO_ORACLE or "")
def test_updatepk_and_tokens_decode(tmp_path):
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=TS)
    body += rec.write_update(KEYED, before={"ID": "1", "NAME": "A"}, after={"ID": "2", "NAME": "A"},
                             txn_id=5, timestamp=TS, txn_tokens=True)
    body += rec.write_insert(KEYED, {"ID": "3", "NAME": "C"}, 6, TS, txn_tokens=True)
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    (trail_dir / "rt0000000").write_bytes(body)
    write_def_file(tmp_path / "w.def", [KEYED])
    records = _ggtrail_harness.decode_dir(trail_dir, tmp_path / "w.def")
    assert [r["op_type"] for r in records] == ["UPDATEPK", "INSERT"]
    assert records[0]["columns"] == {"ID": "2", "NAME": "A"}
    assert records[1]["columns"] == {"ID": "3", "NAME": "C"}
