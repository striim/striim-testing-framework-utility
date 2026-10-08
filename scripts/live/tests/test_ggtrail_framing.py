from __future__ import annotations
import datetime
import shutil
import struct
from livetest.ggtrail.defwriter import ColumnSpec, TableSchema, ASCII_V, ASCII_F, SINT64
from livetest.ggtrail.header import write_header
from livetest.ggtrail.record import write_insert, write_truncate
from livetest.ggtrail.writer import TrailFileWriter

# Hermetic structural checks -- no Java oracle, no STRIIM_HOME. These assert the framing
# invariants the real decoder enforces (Evidence §C: GGBinaryResultSet.findValidHeaderOffset
# reads L at bytes 2-3, requires 'Z' at L-4 and the same L repeated at L-2; Evidence §D:
# per-column [colIndex][colLen][span] with a type-dependent inner prefix), so a byte-level
# regression is caught on any machine even though only the Mac can run the oracle.

_G = 0x47
_F = 0x46
_Z = 0x5A
_D = 0x44

SCHEMA = TableSchema("SCOTT.WIDGETS", [ColumnSpec("ID", ASCII_V, 10), ColumnSpec("NAME", ASCII_V, 50)])
ORDERS = TableSchema("SCOTT.ORDERS", [ColumnSpec("ID", ASCII_V, 10, is_key=True),
                                      ColumnSpec("WIDGET_ID", ASCII_V, 10)])
TYPED = TableSchema("SCOTT.TYPED", [
    ColumnSpec("ID", ASCII_V, 10, is_key=True),
    ColumnSpec("CODE", ASCII_F, 4),
    ColumnSpec("AMOUNT", SINT64, 22, scale=3),
    ColumnSpec("NOTE", ASCII_V, 50),
])

TS = datetime.datetime(2026, 7, 18, 12, 0, 0)


def _u16(b: bytes, off: int) -> int:
    return struct.unpack(">H", b[off:off + 2])[0]


def _split_records(blob: bytes) -> list[bytes]:
    """Walk a trail file the way the decoder does: total length L lives at bytes 2-3."""
    out = []
    pos = 0
    while pos < len(blob):
        length = _u16(blob, pos + 2)
        assert length >= 12, f"implausible record length {length} at offset {pos}"
        out.append(blob[pos:pos + length])
        pos += length
    return out


def _assert_framing(record: bytes, token: int) -> None:
    assert record[0] == token, f"expected token {token:#x} at byte 0, got {record[0]:#x}"
    assert record[1] != 3, "byte 1 must not be 3 (RecordUtil.isRepairRecord)"
    declared = _u16(record, 2)
    assert declared == len(record), f"bytes 2-3 say {declared}, record is {len(record)} bytes"
    assert record[-4] == _Z, "expected 'Z' at L-4"
    assert _u16(record, len(record) - 2) == declared, "trailing u16 must repeat the total length"


def _columns(record: bytes) -> list[tuple[int, int, bytes]]:
    """Decode the ['D'][pad][u16 dataLen] chunk into [(colIndex, colLen, span), ...]."""
    header_len = _u16(record, 6)
    chunk = record[8 + header_len:-4]
    assert chunk[0] == _D, "data chunk must start with the 'D' RECORD_DATA_TOKEN"
    data_len = _u16(chunk, 2)
    body = chunk[4:4 + data_len]
    assert len(body) == data_len, "dataLen must count exactly the column bytes"
    cols, pos = [], 0
    while pos < len(body):
        idx = _u16(body, pos)
        col_len = _u16(body, pos + 2)
        span = body[pos + 4:pos + 4 + col_len]
        assert len(span) == col_len, "column span truncated"
        cols.append((idx, col_len, span))
        pos += 4 + col_len
    return cols


def test_insert_record_framing():
    rec = write_insert(SCHEMA, {"ID": "42", "NAME": "COMPANY 1"}, txn_id=1, timestamp=TS)
    _assert_framing(rec, _G)
    # headerLen = 34 (table-name offset) + 1 NUL + len(table name)
    assert _u16(rec, 6) == 35 + len(b"SCOTT.WIDGETS")
    assert rec[8 + 34:8 + 34 + 13] == b"SCOTT.WIDGETS"


def test_header_record_uses_the_same_framing():
    hdr = write_header(uri="uri:test", filename="rt0000000", seqno=0, creation_time=TS)
    _assert_framing(hdr, _F)
    # TRAIL_INFO section (code 48) sits at byte 8, and its first sub-token is the signature.
    assert hdr[8] == 48
    assert b"GG\r\nTL\n\r" in hdr


def test_ascii_v_and_ascii_f_span_lengths():
    rec = write_insert(TYPED, {"ID": "1", "CODE": "0916", "NOTE": "hello"}, txn_id=1, timestamp=TS)
    cols = dict((idx, (col_len, span)) for idx, col_len, span in _columns(rec))
    # ASCII_V: colLen = strlen + 4, span = [00 00][u16 strlen][string]  (decode reads len-4)
    id_len, id_span = cols[0]
    assert id_len == len(b"1") + 4
    assert id_span == b"\x00\x00\x00\x01" + b"1"
    # ASCII_F: colLen = strlen + 2, span = [00 00][string]  (decode reads len-2)
    code_len, code_span = cols[1]
    assert code_len == len(b"0916") + 2
    assert code_span == b"\x00\x00" + b"0916"


def test_sint64_span_is_always_ten_bytes_and_scaled():
    rec = write_insert(TYPED, {"ID": "1", "AMOUNT": 2.2}, txn_id=1, timestamp=TS)
    cols = dict((idx, (col_len, span)) for idx, col_len, span in _columns(rec))
    amount_len, amount_span = cols[2]
    assert amount_len == 10, "SINT64 colLen is [u16 null-ind] + 8-byte integer"
    assert len(amount_span) == 10
    assert amount_span[:2] == b"\x00\x00"
    # scale 3 -> the wire integer is value * 10^3 (rt000002's AUTH_AMOUNT 0x0898 = 2200 -> 2.200)
    assert struct.unpack(">q", amount_span[2:])[0] == 2200


def test_null_column_span_is_ff_ff():
    # ColumnDefinition.java:110 -- isNull iff BOTH of the first two bytes are non-zero.
    rec = write_insert(TYPED, {"ID": "1", "NOTE": None}, txn_id=1, timestamp=TS)
    cols = dict((idx, (col_len, span)) for idx, col_len, span in _columns(rec))
    note_len, note_span = cols[3]
    assert note_len == 2
    assert note_span == b"\xff\xff"


def test_truncate_record_has_no_columns_but_valid_framing():
    rec = write_truncate(SCHEMA, txn_id=7, timestamp=TS)
    _assert_framing(rec, _G)
    assert _columns(rec) == []


def test_writer_emits_one_def_with_both_tables_and_interleaves_records(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA, ORDERS], clock=lambda: TS)
    w.insert("SCOTT.WIDGETS", {"ID": "1", "NAME": "bolt"})
    w.insert("SCOTT.ORDERS", {"ID": "9", "WIDGET_ID": "1"})
    w.delete("SCOTT.WIDGETS", {"ID": "1", "NAME": "bolt"})
    w.close()

    def_text = (tmp_path / "schema.def").read_text()
    assert "Definition for table SCOTT.WIDGETS" in def_text
    assert "Definition for table SCOTT.ORDERS" in def_text
    assert def_text.count("End of definition") == 2

    records = _split_records((tmp_path / "rt0000000").read_bytes())
    _assert_framing(records[0], _F)                       # the file header
    data = records[1:]
    assert len(data) == 3
    for rec in data:
        _assert_framing(rec, _G)
    names = [rec[8 + 34:8 + 34 + (_u16(rec, 6) - 35)].decode() for rec in data]
    assert names == ["SCOTT.WIDGETS", "SCOTT.ORDERS", "SCOTT.WIDGETS"]
    # Opcodes at header-region offset 6 (= record byte 14): INSERT 5, DELETE 3.
    assert [rec[8 + 6] for rec in data] == [5, 5, 3]


def test_transaction_parts_and_shared_txn_id(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA, ORDERS], txn_id_start=77, clock=lambda: TS)
    w.transaction([
        ("SCOTT.WIDGETS", "insert", {"ID": "1", "NAME": "bolt"}),
        ("SCOTT.ORDERS", "insert", {"ID": "9", "WIDGET_ID": "1"}),
        ("SCOTT.ORDERS", "update", {"ID": "9", "WIDGET_ID": "1"}, {"ID": "9", "WIDGET_ID": "2"}),
    ])
    w.close()

    data = _split_records((tmp_path / "rt0000000").read_bytes())[1:]
    assert len(data) == 3
    # txn id at header-region offset 20 (record byte 28), part at offset 28 (record byte 36).
    txn_ids = [struct.unpack(">Q", rec[8 + 20:8 + 28])[0] for rec in data]
    assert txn_ids == [77, 77, 77]
    parts = [rec[8 + 28] for rec in data]
    assert parts == [0, 1, 2], "TransactionPart BEGIN/MIDDLE/END"


def test_single_op_transaction_is_marked_sole(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA], clock=lambda: TS)
    w.transaction([("SCOTT.WIDGETS", "insert", {"ID": "1", "NAME": "bolt"})])
    w.close()
    data = _split_records((tmp_path / "rt0000000").read_bytes())[1:]
    assert [rec[8 + 28] for rec in data] == [3], "TransactionPart SOLE"


def test_standalone_ops_are_sole_and_consume_one_txn_id_each(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA], txn_id_start=5, clock=lambda: TS)
    w.insert("SCOTT.WIDGETS", {"ID": "1", "NAME": "a"})
    w.insert("SCOTT.WIDGETS", {"ID": "2", "NAME": "b"})
    w.close()
    data = _split_records((tmp_path / "rt0000000").read_bytes())[1:]
    assert [struct.unpack(">Q", rec[8 + 20:8 + 28])[0] for rec in data] == [5, 6]
    assert [rec[8 + 28] for rec in data] == [3, 3]


def test_writer_rolls_over_by_record_count(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA], max_records_per_file=2, clock=lambda: TS)
    for i in range(5):
        w.insert("SCOTT.WIDGETS", {"ID": str(i), "NAME": f"widget-{i}"})
    w.close()
    trail_files = sorted(tmp_path.glob("rt0000*"))
    assert [p.name for p in trail_files] == ["rt0000000", "rt0000001", "rt0000002"]
    counts = [len(_split_records(p.read_bytes())) - 1 for p in trail_files]   # minus the header
    assert counts == [2, 2, 1]


def test_unknown_table_raises(tmp_path):
    w = TrailFileWriter(tmp_path, [SCHEMA], clock=lambda: TS)
    try:
        try:
            w.insert("SCOTT.NOPE", {"ID": "1"})
        except KeyError as exc:
            assert "SCOTT.NOPE" in str(exc)
        else:
            raise AssertionError("expected KeyError for an unknown table")
    finally:
        w.close()


def test_injected_clock_makes_output_byte_deterministic(tmp_path):
    # Spec §12.2: with a fixed clock the only nondeterministic input is gone, so the same
    # op sequence yields byte-identical files. Same directory both runs -- the header's
    # URI embeds the directory path, so comparing across two paths would differ by design.
    out = tmp_path / "gen"

    def run() -> bytes:
        if out.exists():
            shutil.rmtree(out)
        ticks = iter([datetime.datetime(2026, 7, 18, 0, 0, 0) + datetime.timedelta(seconds=n)
                      for n in range(100)])
        w = TrailFileWriter(out, [SCHEMA], clock=lambda: next(ticks))
        w.insert("SCOTT.WIDGETS", {"ID": "1", "NAME": "a"})
        w.insert("SCOTT.WIDGETS", {"ID": "2", "NAME": "b"})
        w.close()
        return (out / "rt0000000").read_bytes()

    assert run() == run()
