from __future__ import annotations
import datetime
from . import tlv

# Trail file header -- Evidence appendix §A. Structure: 'F' token, 2 reserved bytes,
# 2-byte BE region length, then a sequence of section TLVs keyed by
# HeaderInfo.HeaderInfoItems (HeaderInfo.java:19-42). We emit only TRAIL_INFO with the
# fields GGTrailReader/GGResultSet actually need to open and tail the file
# (SIGNATURE, COMPATIBILITY, CREATIONTIME, URI, FILENAME, ISTRAIL, SEQNO) -- MachineInfo/
# DatabaseInfo/ProducerInfo/ContinuityInfo are optional per HeaderInfo's parse loop
# (each section is independently length-framed, so omitting one just means the loop
# never encounters that code) and are added only if a round-trip test requires it.

_TRAIL_INFO_TOKEN = 0x46          # 'F', TrailConstants.TRAIL_INFO_TOKEN
_SIGNATURE = b"GG\r\nTL\n\r"      # TrailInfo.java:16 SIGNATURE token payload, confirmed via hexdump

# TrailInfo.java:16-29 sub-token codes
_CODE_SIGNATURE = 48
_CODE_COMPATIBILITY = 49
_CODE_CREATIONTIME = 51
_CODE_URI = 52
_CODE_FILENAME = 54
_CODE_ISTRAIL = 55
_CODE_SEQNO = 56


def write_header(uri: str, filename: str, seqno: int, creation_time: datetime.datetime,
                 compatibility: int = 8) -> bytes:
    trail_info_payload = (
        tlv.sub_token(_CODE_SIGNATURE, tlv.tchar(_SIGNATURE))
        + tlv.sub_token(_CODE_COMPATIBILITY, tlv.u16(compatibility))
        + tlv.sub_token(_CODE_CREATIONTIME, tlv.tjulian(creation_time))
        + tlv.sub_token(_CODE_URI, tlv.tvarchar(uri))
        + tlv.sub_token(_CODE_FILENAME, tlv.tvarchar(filename))
        + tlv.sub_token(_CODE_ISTRAIL, tlv.u8(1))
        + tlv.sub_token(_CODE_SEQNO, tlv.u32(seqno))
    )
    # HeaderInfo section code for TRAIL_INFO is 48 (HeaderInfo.java:19-42) -- same
    # numeric space as TrailInfo's own sub-tokens, but a different token list.
    header_region = tlv.section(48, trail_info_payload)   # the TLV section(s), placed at offset 8
    header_len = len(header_region)

    # CORRECTED 2026-07-14 review: the 'F' header record is consumed by the SAME framing
    # path as data records (GGBinaryResultSet.findValidHeaderOffset accepts 'F' and 'G'),
    # so it MUST carry the total-length field at bytes 2-3 and end with ['Z'][pad][u16 L].
    # Layout: [F][0][u16 L][u16 0][u16 headerLen][header region][Z][pad][u16 L].
    # (Whether a 'D'/dataLen chunk must sit between the region and 'Z' is a round-trip harness
    #  item; rt000000 packs everything into TLV sections up to the 'Z', so start with none.)
    record_len = 8 + header_len + 4
    out = bytearray()
    out += tlv.u8(_TRAIL_INFO_TOKEN)   # byte 0: 'F'
    out += tlv.u8(0)                   # byte 1: 0 -> GGResultSet reads BigEndian for the trail
    out += tlv.u16(record_len)         # bytes 2-3: total length L
    out += tlv.u16(0)                  # bytes 4-5: unverified field (real header: 0x3000)
    out += tlv.u16(header_len)         # bytes 6-7: headerLen
    out += header_region               # bytes 8..: TLV sections
    out += tlv.u8(0x5A)                # 'Z' at L-4
    out += tlv.u8(0)                   # pad at L-3
    out += tlv.u16(record_len)         # u16 L at L-2
    return bytes(out)
