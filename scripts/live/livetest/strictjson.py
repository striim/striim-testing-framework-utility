"""Strict JSON payload decoding, with exact numeric tokens and duplicate-key refusal."""
import json
from decimal import Decimal


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


def decoder():
    return json.JSONDecoder(parse_float=Decimal, object_pairs_hook=_pairs, parse_constant=_constant)


def loads(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return decoder().decode(raw)


def events(text):
    dec, out, pos = decoder(), [], 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos == len(text):
            break
        obj, pos = dec.raw_decode(text, pos)
        values = obj if isinstance(obj, list) else [obj]
        if any(not isinstance(value, dict) for value in values):
            raise ValueError("JSON events must be objects")
        out.extend(values)
    return out
