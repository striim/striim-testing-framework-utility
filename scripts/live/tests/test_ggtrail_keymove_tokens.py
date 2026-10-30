"""An UPDATE that changes the key is written as UPDATEPK with its before-image key, and
txn_tokens puts GoldenGate's TxnID and CSN tokens on a transaction's first record (FIRST, as
GoldenGate does) or on every record (EVERY)."""
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
FIRST, EVERY = rec.TXN_TOKENS_FIRST, rec.TXN_TOKENS_EVERY


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


def _tokens(txn_id: int) -> bytes:
    value = str(txn_id).encode()
    return b"".join(bytes([code, 0]) + struct.pack(">H", len(value)) + value
                    for code in (rec._TOKEN_TXNID, rec._TOKEN_CSN))


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


def test_same_key_no_before_or_no_key_column_stays_a_plain_update():
    same = rec.write_update(KEYED, before={"ID": "1", "NAME": "A"}, after={"ID": "1", "NAME": "B"},
                            txn_id=7, timestamp=TS)
    none = rec.write_update(KEYED, before={}, after={"ID": "1", "NAME": "B"}, txn_id=7, timestamp=TS)
    unkeyed = TableSchema("SCOTT.W", [ColumnSpec("ID", ASCII_V, 10), ColumnSpec("NAME", ASCII_V, 50)])
    moved = rec.write_update(unkeyed, before={"ID": "1"}, after={"ID": "2", "NAME": "B"},
                             txn_id=7, timestamp=TS)
    assert _io_type(same) == _io_type(none) == _io_type(moved) == rec._DBOP_UPDATE


def test_keys_compare_as_written_not_as_python_values():
    sint = TableSchema("SCOTT.N", [ColumnSpec("ID", rec._SINT64, 19, is_key=True),
                                   ColumnSpec("NAME", ASCII_V, 50)])
    body = rec.write_update(sint, before={"ID": "1", "NAME": "A"}, after={"ID": 1, "NAME": "B"},
                            txn_id=7, timestamp=TS)
    assert _io_type(body) == rec._DBOP_UPDATE


def test_a_before_image_without_the_whole_key_is_refused():
    with pytest.raises(ValueError, match=r"lacks key column\(s\) \['ID'\]"):
        rec.write_update(KEYED, before={"NAME": "A"}, after={"ID": "2", "NAME": "A"},
                         txn_id=7, timestamp=TS)


def test_first_tokens_on_begin_and_sole_records_only():
    parts = {rec._TXN_PART_BEGIN: True, rec._TXN_PART_MIDDLE: False,
             rec._TXN_PART_END: False, rec._TXN_PART_SOLE: True}
    for part, tokened in parts.items():
        r = rec.write_insert(KEYED, {"ID": "1", "NAME": "A"}, 9, TS, txn_part=part, txn_tokens=FIRST)
        assert _chunks(r)[1] == (_tokens(9) if tokened else None), part


def test_every_tokens_on_every_record_and_none_by_default():
    for part in (rec._TXN_PART_BEGIN, rec._TXN_PART_MIDDLE, rec._TXN_PART_END, rec._TXN_PART_SOLE):
        r = rec.write_delete(KEYED, {"ID": "3"}, 9, TS, txn_part=part, txn_tokens=EVERY)
        assert _chunks(r)[1] == _tokens(9), part
        off = rec.write_delete(KEYED, {"ID": "3"}, 9, TS, txn_part=part)
        assert _chunks(off)[1] is None
        # the trailing length still equals the leading one
        for x in (r, off):
            assert x[2:4] == x[-2:] and len(x) == struct.unpack(">H", x[2:4])[0]


def test_an_unknown_token_mode_is_refused():
    with pytest.raises(ValueError, match="txn_tokens must be one of"):
        rec.write_insert(KEYED, {"ID": "1", "NAME": "A"}, 9, TS, txn_tokens=True)


def test_write_transaction_takes_txn_tokens():
    body = rec.write_transaction(KEYED, [("insert", {"ID": "1", "NAME": "A"}),
                                         ("insert", {"ID": "2", "NAME": "B"})],
                                 txn_id=4, timestamp=TS, txn_tokens=FIRST)
    first_len = struct.unpack(">H", body[2:4])[0]
    assert _chunks(body[:first_len])[1] == _tokens(4)
    assert _chunks(body[first_len:])[1] is None


@pytest.mark.parametrize("mode", [FIRST, EVERY])
def test_writer_passes_txn_tokens_through(tmp_path, mode):
    w = TrailFileWriter(tmp_path, [KEYED], clock=lambda: TS, txn_tokens=mode)
    w.transaction([("SCOTT.WIDGETS", "update", {"ID": "1", "NAME": "A"}, {"ID": "2", "NAME": "A"}),
                   ("SCOTT.WIDGETS", "update", {"ID": "2", "NAME": "B"}, {"ID": "1", "NAME": "B"})])
    w.close()
    data = (tmp_path / "rt0000000").read_bytes()
    for before, after, part, tokened in (
            ({"ID": "1", "NAME": "A"}, {"ID": "2", "NAME": "A"}, rec._TXN_PART_BEGIN, True),
            ({"ID": "2", "NAME": "B"}, {"ID": "1", "NAME": "B"}, rec._TXN_PART_END, mode == EVERY)):
        r = rec.write_update(KEYED, before, after, 1, TS, txn_part=part, txn_tokens=mode)
        assert r in data and (_chunks(r)[1] is not None) == tokened, part


_NO_ORACLE = (None if os.environ.get("STRIIM_HOME") else "requires STRIIM_HOME for the Java oracle") \
    or _ggtrail_harness.missing_reason()


@pytest.mark.skipif(_NO_ORACLE is not None, reason=_NO_ORACLE or "")
def test_updatepk_and_tokens_decode(tmp_path):
    body = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=TS)
    body += rec.write_update(KEYED, before={"ID": "1", "NAME": "A"}, after={"ID": "2", "NAME": "A"},
                             txn_id=5, timestamp=TS, txn_tokens=FIRST)
    body += rec.write_insert(KEYED, {"ID": "3", "NAME": "C"}, 6, TS, txn_part=rec._TXN_PART_MIDDLE,
                             txn_tokens=FIRST)
    trail_dir = tmp_path / "trail"
    trail_dir.mkdir()
    (trail_dir / "rt0000000").write_bytes(body)
    write_def_file(tmp_path / "w.def", [KEYED])
    records = _ggtrail_harness.decode_dir(trail_dir, tmp_path / "w.def")
    assert [r["op_type"] for r in records] == ["UPDATEPK", "INSERT"]
    assert records[0]["columns"] == {"ID": "2", "NAME": "A"}
    assert records[1]["columns"] == {"ID": "3", "NAME": "C"}
    # A harness that reports the before-image and the system tokens (field's does from
    # 2026-10-30) shows the parser read the key and the tokens as written.
    if "before" in records[0]:
        assert records[0]["before"] == {"ID": "1"}
    if "tokens" in records[0]:
        assert records[0]["tokens"] == {"TxnID": "5", "CSN": "5"}
        assert records[1]["tokens"] == {}
