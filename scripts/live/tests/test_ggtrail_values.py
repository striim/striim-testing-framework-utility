from __future__ import annotations
import datetime
import decimal
import random
import re
import uuid

import pytest

from livetest.ggtrail.model import Column
from livetest.ggtrail.values import generate, generate_value, mutate

# Tier 1 (spec §10) -- R10: every generated value conforms to its column's declared logical
# type, and an update's mutation always produces a DIFFERENT value (the precondition for
# R9's "an update changes something").

_ALL_DTYPES = [
    "int",
    "decimal(10,2)",
    "decimal(6,0)",
    "varchar(20)",
    "char(8)",
    "uuid",
    "date",
    "timestamp",
]


def _col(dtype, **kw):
    return Column(name="C", dtype=dtype, **kw)


def _conforms(dtype: str, value) -> None:
    """The reference type-checker: parse the value back as its declared logical type."""
    if dtype == "int":
        assert isinstance(value, int) and not isinstance(value, bool)
    elif dtype.startswith("decimal"):
        precision, scale = (int(x) for x in dtype[len("decimal(") : -1].split(","))
        text = str(value)
        parsed = decimal.Decimal(text)
        assert (
            -parsed.as_tuple().exponent == scale
        ), f"{text} does not carry exactly {scale} dp"
        assert len(parsed.as_tuple().digits) <= precision or parsed == 0
    elif dtype.startswith("varchar"):
        n = int(dtype[len("varchar(") : -1])
        assert isinstance(value, str) and 0 < len(value) <= n
    elif dtype.startswith("char"):
        n = int(dtype[len("char(") : -1])
        assert isinstance(value, str) and len(value) == n
    elif dtype == "uuid":
        assert uuid.UUID(value).version == 4
    elif dtype == "date":
        datetime.datetime.strptime(value, "%Y-%m-%d")
    elif dtype == "timestamp":
        datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    else:  # pragma: no cover
        raise AssertionError(f"no reference check for {dtype!r}")


# --- generation conforms to the declared type ---------------------------------------------


@pytest.mark.parametrize("dtype", _ALL_DTYPES)
def test_generated_values_conform_to_their_logical_type(dtype):
    rng = random.Random(4)
    for _ in range(400):
        _conforms(dtype, generate_value(dtype, rng))


def test_decimal_scale_is_exact_not_merely_close():
    # A float would render 0.10 as "0.1" and silently break a textual golden compare, so
    # decimals are generated as strings that keep every trailing zero.
    rng = random.Random(9)
    for _ in range(300):
        text = generate_value("decimal(8,3)", rng)
        assert re.match(r"^\d+\.\d{3}$", text), text


def test_zero_scale_decimal_has_no_decimal_point():
    rng = random.Random(9)
    assert all("." not in generate_value("decimal(6,0)", rng) for _ in range(50))


def test_char_is_padded_to_exactly_its_width():
    rng = random.Random(2)
    for n in (1, 4, 8, 20):
        for _ in range(50):
            value = generate_value(f"char({n})", rng)
            assert len(value) == n
            assert value == value.rstrip().ljust(n)  # padding is trailing spaces only


def test_varchar_never_exceeds_its_width_and_is_never_empty():
    # Never empty on purpose: an empty string reads back from a CSV golden exactly like a
    # NULL, which would make the NULL assertions below meaningless.
    rng = random.Random(2)
    for n in (1, 3, 12, 60):
        for _ in range(80):
            value = generate_value(f"varchar({n})", rng)
            assert 0 < len(value) <= n


def test_dates_and_timestamps_fall_inside_the_fixed_window():
    # A FIXED window, not "now" -- a regenerated fixture must not change meaning next year.
    rng = random.Random(6)
    lo, hi = datetime.datetime(2020, 1, 1), datetime.datetime(2030, 1, 2)
    for _ in range(300):
        moment = datetime.datetime.strptime(
            generate_value("timestamp", rng), "%Y-%m-%d %H:%M:%S"
        )
        assert lo <= moment <= hi


def test_generation_is_deterministic_per_seed():
    for dtype in _ALL_DTYPES:
        a = [generate_value(dtype, random.Random(21)) for _ in range(1)]
        b = [generate_value(dtype, random.Random(21)) for _ in range(1)]
        assert a == b
    assert [generate_value("int", random.Random(1)) for _ in range(10)] != [
        generate_value("int", random.Random(2)) for _ in range(10)
    ]


def test_unknown_logical_type_is_rejected():
    with pytest.raises(ValueError):
        generate_value("blob", random.Random(0))


# --- NULLs ---------------------------------------------------------------------------------


def test_non_nullable_columns_never_produce_null():
    rng = random.Random(8)
    col = _col("varchar(10)", nullable=False, null_rate=0.9)  # null_rate is ignored
    assert all(generate(col, rng) is not None for _ in range(500))


@pytest.mark.parametrize("rate", [0.0, 0.25, 0.5, 0.9])
def test_nullable_columns_emit_nulls_at_about_their_null_rate(rate):
    rng = random.Random(13)
    col = _col("varchar(10)", nullable=True, null_rate=rate)
    n = 4000
    nulls = sum(1 for _ in range(n) if generate(col, rng) is None)
    assert abs(nulls / n - rate) < 0.03, f"{nulls}/{n} nulls for null_rate={rate}"


def test_non_null_values_from_a_nullable_column_still_conform():
    rng = random.Random(13)
    col = _col("decimal(9,2)", nullable=True, null_rate=0.5)
    for _ in range(400):
        value = generate(col, rng)
        if value is not None:
            _conforms("decimal(9,2)", value)


# --- mutation --------------------------------------------------------------------------------


@pytest.mark.parametrize("dtype", _ALL_DTYPES)
def test_mutate_always_returns_a_different_conforming_value(dtype):
    rng = random.Random(17)
    col = _col(dtype)
    old = generate_value(dtype, rng)
    for _ in range(400):
        new = mutate(col, old, rng)
        assert new != old
        _conforms(dtype, new)
        old = new


def test_mutate_changes_even_a_single_character_domain():
    # char(1) has a small alphabet, so the random redraw collides often; the bounded retry
    # plus deterministic perturbation must still guarantee a change.
    rng = random.Random(19)
    col = _col("char(1)")
    old = "A"
    for _ in range(500):
        new = mutate(col, old, rng)
        assert new != old and len(new) == 1
        old = new


def test_mutate_can_flip_a_nullable_column_to_null_and_back():
    rng = random.Random(23)
    col = _col("varchar(10)", nullable=True, null_rate=0.5)
    seen_null = seen_value = False
    old = "alpha"
    for _ in range(400):
        new = mutate(col, old, rng)
        assert new != old
        seen_null |= new is None
        seen_value |= new is not None
        old = new
    assert seen_null and seen_value


def test_mutate_from_null_produces_a_value():
    rng = random.Random(29)
    col = _col("int", nullable=True, null_rate=0.5)
    assert all(mutate(col, None, rng) is not None for _ in range(200))
