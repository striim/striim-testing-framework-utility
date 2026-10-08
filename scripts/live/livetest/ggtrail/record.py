from __future__ import annotations
import datetime
import struct
from . import tlv
from .defwriter import ColumnSpec, TableSchema

# Per-record + per-column framing -- Evidence appendix §C/§D.
_RECORD_BEGIN_TOKEN = 0x47   # 'G'
_RECORD_END_TOKEN = 0x5A     # 'Z'
_BEFORE = 0x42               # 'B'
_AFTER = 0x41                # 'A'

_DBOP_INSERT = 5
_DBOP_DELETE = 3
_DBOP_UPDATE = 10
_DBOP_TRUNCATE = 100

_HEADER_DATA_BEGIN_OFFSET = 8
_TABLENAME_OFFSET = 34
# Header region (bytes 8..8+headerLen) layout, offsets relative to HEADER_DATA_BEGIN_OFFSET:
_BEFORE_AFTER_OFFSET = 3
_RECORD_LENGTH_OFFSET = 4
_OPERATION_TYPE_OFFSET = 6
_TIMESTAMP_OFFSET = 8
_TRANSACTION_ID_OFFSET = 20
_TRANSACTION_PART_OFFSET = 28
_RECORD_INCOMPLETE_OFFSET = 31
_RECORD_CONTINUED_OFFSET = 32

_TXN_PART_SOLE = 3  # TransactionPart enum: BEGIN=0, MIDDLE=1, END=2, SOLE=3
_TXN_PART_BEGIN, _TXN_PART_MIDDLE, _TXN_PART_END = 0, 1, 2


_RECORD_DATA_TOKEN = 0x44    # 'D'
_ASCII_F = 0
_ASCII_V = 64
_SINT64 = 134


def _encode_column(index: int, col: ColumnSpec, value) -> bytes:
    # [colIndex: u16 BE][colLen: u16 BE][value span] -- colLen counts the type-dependent
    # inner prefix (Evidence §D, corrected). convertBytes(span, 0, colLen) decodes it.
    # Takes the whole ColumnSpec (not just gg_type) because SINT64 needs col.scale.
    if value is None:
        span = b"\xff\xff"                                  # NULL: both null-ind bytes non-zero
    elif col.gg_type == _ASCII_V:
        b = value.encode("utf-8")
        span = b"\x00\x00" + tlv.u16(len(b)) + b            # [null-ind][innerLen][str], decode uses len-4
    elif col.gg_type == _ASCII_F:
        b = value.encode("utf-8")
        span = b"\x00\x00" + b                              # [null-ind][str], decode uses len-2
    elif col.gg_type == _SINT64:
        # ColumnDefinition.java: bytesToNumber(span, pos+2, min(len-2, 8), scale) --
        # span = [u16 null-ind = 0][8-byte BE signed int], colLen = 10, decoded value is
        # the binary integer scaled by 10^-scale. Verified against rt000002's AUTH_AMOUNT
        # (0x0898 = 2200, scale 3 -> 2.200). Accepts int/float/str on the Python side.
        span = b"\x00\x00" + struct.pack(">q", round(float(value) * 10 ** col.scale))
    else:
        raise NotImplementedError(f"column wire encoding for gg_type {col.gg_type} not yet supported "
                                  "(DATETIME deferred -- see Evidence §D / out-of-scope)")
    return tlv.u16(index) + tlv.u16(len(span)) + span


def _build_record(table: TableSchema, opcode: int, before_after: int,
                  values: dict, txn_id: int, timestamp: datetime.datetime,
                  txn_part: int = _TXN_PART_SOLE) -> bytes:
    columns = b"".join(
        _encode_column(i, col, values[col.name])
        for i, col in enumerate(table.columns) if col.name in values
    )
    # Data chunk: ['D'][1 pad][u16 dataLen][columns] -- dataLen at chunk offset 2, counts
    # only the column bytes (Evidence §C/§D, corrected: NOT [u32 pad][u16 len]).
    data_chunk = bytes([_RECORD_DATA_TOKEN, 0x00]) + tlv.u16(len(columns)) + columns

    table_name = table.name.encode("ascii")
    header_region = bytearray(_TABLENAME_OFFSET + 1 + len(table_name))
    header_region[_BEFORE_AFTER_OFFSET] = before_after
    header_region[_OPERATION_TYPE_OFFSET] = opcode & 0xFF
    header_region[_TIMESTAMP_OFFSET:_TIMESTAMP_OFFSET + 8] = tlv.tjulian(timestamp)
    header_region[_TRANSACTION_ID_OFFSET:_TRANSACTION_ID_OFFSET + 8] = tlv.u64(txn_id)
    header_region[_TRANSACTION_PART_OFFSET] = txn_part
    header_region[_RECORD_INCOMPLETE_OFFSET] = 0
    header_region[_RECORD_CONTINUED_OFFSET] = 0
    header_region[_TABLENAME_OFFSET:_TABLENAME_OFFSET + len(table_name)] = table_name
    header_region[_TABLENAME_OFFSET + len(table_name)] = 0

    header_len = len(header_region)                          # = 35 + len(table_name)
    body = bytes(header_region) + data_chunk
    # Record layout (Evidence §C, corrected): bytes 0='G',1=0,2-3=L,4-5=0(unverified),
    # 6-7=headerLen, 8..=body, then ['Z'][1 pad][u16 L]. Total L = 8 + len(body) + 4.
    record_len = _HEADER_DATA_BEGIN_OFFSET + len(body) + 4   # 'Z' + pad + u16 trailing length

    out = bytearray()
    out += tlv.u8(_RECORD_BEGIN_TOKEN)   # byte 0: 'G'
    out += tlv.u8(0)                     # byte 1: reserved (must not be 3 = repair token)
    out += tlv.u16(record_len)           # bytes 2-3: total record length L
    out += tlv.u16(0)                    # bytes 4-5: unverified field (real: 0x4800); 0 pending format verification
    out += tlv.u16(header_len)           # bytes 6-7: headerLen (HEADER_BEGIN_OFFSET+2)
    out += body                          # bytes 8..: header region + data chunk
    out += tlv.u8(_RECORD_END_TOKEN)     # 'Z' at L-4
    out += tlv.u8(0)                     # pad byte at L-3 (reader ignores its value)
    out += tlv.u16(record_len)           # u16 L at L-2 (trailing length, must equal bytes 2-3)
    return bytes(out)


def write_insert(table: TableSchema, values: dict, txn_id: int, timestamp: datetime.datetime,
                 txn_part: int = _TXN_PART_SOLE) -> bytes:
    return _build_record(table, _DBOP_INSERT, _AFTER, values, txn_id, timestamp, txn_part=txn_part)


def write_delete(table: TableSchema, values: dict, txn_id: int, timestamp: datetime.datetime,
                 txn_part: int = _TXN_PART_SOLE) -> bytes:
    return _build_record(table, _DBOP_DELETE, _BEFORE, values, txn_id, timestamp, txn_part=txn_part)


def write_update(table: TableSchema, before: dict, after: dict,
                 txn_id: int, timestamp: datetime.datetime,
                 txn_part: int = _TXN_PART_SOLE) -> bytes:
    # Plain DBOP_UPDATE, after-image only -- `before` is accepted for API symmetry with
    # real CDC diffing but not yet encoded on the wire (RecordUtil's doBefore prefix path
    # is UPDATEPK-specific, Operation.java:208, and out of scope for this task).
    return _build_record(table, _DBOP_UPDATE, _AFTER, after, txn_id, timestamp, txn_part=txn_part)


def write_truncate(table: TableSchema, txn_id: int, timestamp: datetime.datetime,
                   txn_part: int = _TXN_PART_SOLE) -> bytes:
    return _build_record(table, _DBOP_TRUNCATE, _AFTER, {}, txn_id, timestamp, txn_part=txn_part)


def write_transaction(table: TableSchema, ops: list, txn_id: int,
                      timestamp: datetime.datetime) -> bytes:
    # Single-table multi-part transaction: all records share txn_id, parts run
    # BEGIN/MIDDLE.../END (TransactionPart 0/1/2) vs. the SOLE=3 single-op records use.
    # The cross-table equivalent lives on TrailFileWriter.transaction() (spec §12.3).
    opcodes = {"insert": _DBOP_INSERT, "delete": _DBOP_DELETE, "update": _DBOP_UPDATE}
    out = bytearray()
    for i, (kind, values) in enumerate(ops):
        part = _TXN_PART_BEGIN if i == 0 else (_TXN_PART_END if i == len(ops) - 1 else _TXN_PART_MIDDLE)
        out += _build_record(table, opcodes[kind], _AFTER, values, txn_id, timestamp, txn_part=part)
    return bytes(out)
