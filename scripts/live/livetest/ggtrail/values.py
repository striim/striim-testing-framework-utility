from __future__ import annotations
import datetime
import random
import uuid

from .model import Column, parse_dtype

# Typed value generation (spec §4.1, R10). Every value produced here is the LOGICAL value
# the column declares -- Layer 0's encoder is what turns a decimal into its scaled SINT64
# integer, so nothing in this module knows about the wire.
#
# Determinism: every draw comes from the caller's seeded random.Random. No module-level
# rng, no wall clock -- the date/timestamp window below is a fixed constant precisely so a
# generated fixture does not change meaning when it is regenerated next year.

_WORDS = (
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
    "oscar",
    "papa",
    "quebec",
    "romeo",
    "sierra",
    "tango",
    "uniform",
    "victor",
    "whiskey",
    "xray",
    "yankee",
    "zulu",
)
_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Fixed generation window for date/timestamp: 2020-01-01 .. 2029-12-31 (inclusive-ish).
_EPOCH = datetime.datetime(2020, 1, 1, 0, 0, 0)
_WINDOW_SECONDS = 10 * 365 * 24 * 3600

_MUTATE_ATTEMPTS = 64


def generate(col: Column, rng: random.Random):
    """A fresh value for `col`, honouring nullability (None at ~null_rate)."""
    if col.nullable and col.null_rate > 0.0 and rng.random() < col.null_rate:
        return None
    return generate_value(col.dtype, rng)


def generate_value(dtype: str, rng: random.Random):
    """A fresh NON-NULL value for a logical type."""
    base, args = parse_dtype(dtype)
    if base == "int":
        return rng.randint(1, 10**9)
    if base == "decimal":
        return _decimal(args[0], args[1], rng)
    if base == "varchar":
        return _varchar(args[0], rng)
    if base == "char":
        return _char(args[0], rng)
    if base == "uuid":
        return str(uuid.UUID(bytes=rng.randbytes(16), version=4))
    if base == "date":
        return _moment(rng).strftime("%Y-%m-%d")
    if base == "timestamp":
        return _moment(rng).strftime("%Y-%m-%d %H:%M:%S")
    raise ValueError(
        f"no value generator for logical type {dtype!r}"
    )  # pragma: no cover


def mutate(col: Column, old, rng: random.Random):
    """A value for `col` guaranteed to DIFFER from `old` (R9: an update must change
    something, or the workload tests could not tell an update from a no-op).

    A nullable column may flip to NULL, which is itself a change; otherwise the retry
    loop redraws. The loop is bounded because a narrow domain (char(1), say) can redraw
    the same value many times -- on exhaustion we fall back to a deterministic
    perturbation rather than returning `old` and silently breaking the invariant.
    """
    if (
        col.nullable
        and old is not None
        and col.null_rate > 0.0
        and rng.random() < col.null_rate
    ):
        return None
    for _ in range(_MUTATE_ATTEMPTS):
        value = generate_value(col.dtype, rng)
        if value != old:
            return value
    return _perturb(col.dtype, old)


def _perturb(dtype: str, old):
    # Last resort when the random domain kept colliding: shift the value by one within its
    # own type so the result is still type-conformant AND definitely different.
    base, args = parse_dtype(dtype)
    if base == "int":
        return (int(old) + 1) if old is not None else 1
    if base == "decimal":
        precision, scale = args
        step = 10 ** (-scale) if scale else 1
        bumped = round(float(old or 0) + step, scale)
        limit = 10 ** (precision - scale)
        return _format_decimal(bumped % limit if limit else bumped, scale)
    if base == "char":
        n = args[0]
        head = (
            _CHARS[(_CHARS.index(old[0]) + 1) % len(_CHARS)]
            if old and old[0] in _CHARS
            else "A"
        )
        return (head + (old[1:] if old else ""))[:n].ljust(n)
    if base == "varchar":
        n = args[0]
        return (("x" + old) if old else "x")[:n]
    if base == "uuid":
        return (
            str(uuid.UUID(int=(uuid.UUID(old).int + 1) % (1 << 128)))
            if old
            else str(uuid.uuid4())
        )
    if base == "date":
        d = datetime.datetime.strptime(old, "%Y-%m-%d") if old else _EPOCH
        return (d + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if base == "timestamp":
        d = datetime.datetime.strptime(old, "%Y-%m-%d %H:%M:%S") if old else _EPOCH
        return (d + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    raise ValueError(f"no perturbation for logical type {dtype!r}")  # pragma: no cover


def _decimal(precision: int, scale: int, rng: random.Random) -> str:
    # Rendered as a STRING with exactly `scale` fractional digits: the CSV golden and the
    # SINT64 encoder both accept it, and a str keeps the exact scale that a float would
    # lose (0.10 must not print as 0.1 -- the golden is compared textually).
    int_digits = precision - scale
    whole = rng.randint(0, 10**int_digits - 1) if int_digits > 0 else 0
    frac = rng.randint(0, 10**scale - 1) if scale > 0 else 0
    return f"{whole}.{frac:0{scale}d}" if scale > 0 else str(whole)


def _format_decimal(value: float, scale: int) -> str:
    return f"{value:.{scale}f}" if scale > 0 else str(int(value))


def _varchar(n: int, rng: random.Random) -> str:
    # Word-ish and always non-empty: an empty string reads back from CSV identically to a
    # NULL, which would make the NULL assertions untrustworthy.
    out = rng.choice(_WORDS)
    while len(out) + 1 < n and rng.random() < 0.35:
        out = f"{out}_{rng.choice(_WORDS)}"
    if len(out) > n:
        out = out[:n]
    if not out:
        out = _CHARS[rng.randrange(len(_CHARS))]
    return out


def _char(n: int, rng: random.Random) -> str:
    # ASCII_F is fixed width: the value must be EXACTLY n characters, space padded.
    body = "".join(_CHARS[rng.randrange(len(_CHARS))] for _ in range(min(n, 8)))
    return body.ljust(n)[:n]


def _moment(rng: random.Random) -> datetime.datetime:
    return _EPOCH + datetime.timedelta(seconds=rng.randrange(_WINDOW_SECONDS))
