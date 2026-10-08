from __future__ import annotations
import datetime
from . import tlv

# DDL record -- Evidence appendix §E. Wire shape mirrors a normal record header
# (Evidence §C) but with opcode DBOP_DDLOP=160 and a text payload following the
# literal marker "C1=" (DdlOperation.java:158-234's tag grammar).
_RECORD_BEGIN_TOKEN = 0x47
_RECORD_END_TOKEN = 0x5A
_DBOP_DDLOP = 160
_HEADER_DATA_BEGIN_OFFSET = 8
_TABLENAME_OFFSET = 34
_OPERATION_TYPE_OFFSET = 6
_TIMESTAMP_OFFSET = 8
_TRANSACTION_ID_OFFSET = 20
_TRANSACTION_PART_OFFSET = 28
_TXN_PART_SOLE = 3


def write_ddl(schema: str, object_name: str, ddl_text: str, txn_id: int, timestamp: datetime.datetime,
              catalog_object_type: str = "TABLE", operation_name: str = "ALTER") -> bytes:
    tag_text = (
        f"B3='{schema}',B4='{object_name}',B5='{catalog_object_type}',B6='{operation_name}',"
        f"C1={ddl_text}"
    )
    payload = tag_text.encode("utf-8")
    # Same framing as data records (Evidence §C/§E, corrected): ['D'][pad][u16 dataLen][text].
    # DdlOperation.fillDdldata reads dataLen at dataPos+2 then the text at dataPos+4.
    data_chunk = bytes([0x44, 0x00]) + tlv.u16(len(payload)) + payload

    # DDL records aren't tied to a specific table row -- table name field is empty.
    header_region = bytearray(_TABLENAME_OFFSET + 1)
    header_region[_OPERATION_TYPE_OFFSET] = _DBOP_DDLOP & 0xFF
    header_region[_TIMESTAMP_OFFSET:_TIMESTAMP_OFFSET + 8] = tlv.tjulian(timestamp)
    header_region[_TRANSACTION_ID_OFFSET:_TRANSACTION_ID_OFFSET + 8] = tlv.u64(txn_id)
    header_region[_TRANSACTION_PART_OFFSET] = _TXN_PART_SOLE

    header_len = len(header_region)
    body = bytes(header_region) + data_chunk
    record_len = _HEADER_DATA_BEGIN_OFFSET + len(body) + 4   # 'Z' + pad + u16 trailing length

    out = bytearray()
    # bytes: [G][0][u16 L][u16 0 (bytes 4-5)][u16 headerLen][body][Z][pad][u16 L]
    out += tlv.u8(_RECORD_BEGIN_TOKEN) + tlv.u8(0) + tlv.u16(record_len) + tlv.u16(0) + tlv.u16(header_len)
    out += body
    out += tlv.u8(_RECORD_END_TOKEN) + tlv.u8(0) + tlv.u16(record_len)
    return bytes(out)
