from __future__ import annotations
import struct
from .julian import to_julian_long

# Low-level GoldenGate trail TLV / token byte encoders -- the inverse of Token.java's
# decode table (GGTrailParser src/main/java/com/webaction/source/gg/trail/Token.java:13-62)
# and of HeaderInfo.java:69-106's section walk. Big-endian only (see the plan's Global
# Constraints: little-endian trails use a structurally different transaction-id encoding
# and are explicitly out of scope).


def u8(v: int) -> bytes:
    return struct.pack(">B", v)


def u16(v: int) -> bytes:
    return struct.pack(">H", v)


def u32(v: int) -> bytes:
    return struct.pack(">I", v)


def u64(v: int) -> bytes:
    return struct.pack(">Q", v)


def tchar(b: bytes) -> bytes:
    # Token.java:49-51: raw bytes, length carried by the outer TLV length.
    return b


def tvarchar(s: str) -> bytes:
    # Token.java:32-35: own embedded 2-byte BE length + string bytes.
    b = s.encode("utf-8")
    return u16(len(b)) + b


def tchar255(s: str) -> bytes:
    # Token.java:52-55: Pascal-style 1-byte length prefix.
    b = s.encode("utf-8")
    if len(b) > 255:
        raise ValueError(f"TCHAR255 value too long: {len(b)} bytes")
    return u8(len(b)) + b


def tvarchararray(values: list[str]) -> bytes:
    # Token.java:36-48: repeated [2-byte pad][2-byte fullLen][2-byte sLen][sLen bytes],
    # decode walks by fullLen+4; fullLen = len(value)+2.
    out = bytearray()
    for v in values:
        b = v.encode("utf-8")
        out += u16(0) + u16(len(b) + 2) + u16(len(b)) + b
    return bytes(out)


def tjulian(dt) -> bytes:
    return u64(to_julian_long(dt))


def sub_token(code: int, payload: bytes) -> bytes:
    # AbstractTokenList.java:37-67 outer walk: [1-byte code][1-byte pad][2-byte BE len][payload]
    return u8(code) + u8(0) + u16(len(payload)) + payload


def section(code: int, payload: bytes) -> bytes:
    # HeaderInfo.java:69-106 top-level header section framing -- identical 4-byte prefix shape
    return sub_token(code, payload)
