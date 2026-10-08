from __future__ import annotations
import datetime
from livetest.ggtrail import tlv


def test_u16_big_endian():
    assert tlv.u16(9) == b"\x00\x09"


def test_tvarchar_embeds_its_own_length():
    assert tlv.tvarchar("hi") == b"\x00\x02hi"


def test_tchar255_pascal_string():
    assert tlv.tchar255("hi") == b"\x02hi"


def test_tvarchararray_matches_decode_stride():
    # Token.java:36-48 decode: [2-byte pad][2-byte fullLen][2-byte sLen][sLen bytes],
    # stride fullLen+4. fullLen = len(value)+2.
    encoded = tlv.tvarchararray(["ab", "cde"])
    assert encoded == (b"\x00\x00" + b"\x00\x04" + b"\x00\x02" + b"ab"
                       + b"\x00\x00" + b"\x00\x05" + b"\x00\x03" + b"cde")


def test_sub_token_framing_matches_abstracttokenlist_stride():
    payload = b"xyz"
    encoded = tlv.sub_token(48, payload)
    assert encoded == b"\x30\x00\x00\x03xyz"


def test_tjulian_round_trips_via_julian_module():
    from livetest.ggtrail.julian import from_julian_long
    dt = datetime.datetime(2026, 7, 14, 1, 2, 3, 4000)
    encoded = tlv.tjulian(dt)
    assert len(encoded) == 8
    import struct
    assert from_julian_long(struct.unpack(">Q", encoded)[0]) == dt
