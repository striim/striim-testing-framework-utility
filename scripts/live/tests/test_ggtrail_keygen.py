from __future__ import annotations
import random
import re
import uuid

import pytest

from livetest.ggtrail.keygen import (
    KINDS,
    RandomIntKeyGen,
    SequentialKeyGen,
    UuidKeyGen,
    make_keygen,
)

# Tier 1 (spec §10) -- R13 (key generation strategy) and the half of R3 that lives in the
# generators themselves: a key is unique and, because no generator ever rewinds, is never
# reissued after the row that carried it is deleted.

_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _take(gen, n):
    return [gen.next() for _ in range(n)]


# --- sequential ------------------------------------------------------------------------


def test_sequential_counts_up_from_start():
    assert _take(SequentialKeyGen(), 5) == [1, 2, 3, 4, 5]
    assert _take(SequentialKeyGen(start=100, step=5), 4) == [100, 105, 110, 115]


def test_sequential_rejects_a_zero_step():
    with pytest.raises(ValueError, match="non-zero"):
        SequentialKeyGen(step=0)


# --- uuid -------------------------------------------------------------------------------


def test_uuid_keys_are_rfc4122_v4():
    keys = _take(UuidKeyGen(random.Random(7)), 200)
    for key in keys:
        assert _UUID_V4.match(key), key
        assert uuid.UUID(key).version == 4


def test_uuid_keys_are_unique():
    keys = _take(UuidKeyGen(random.Random(7)), 2000)
    assert len(set(keys)) == len(keys)


# --- random int -------------------------------------------------------------------------


def test_random_int_keys_are_unique_and_in_range():
    keys = _take(RandomIntKeyGen(random.Random(3), lo=1, hi=5000), 2000)
    assert len(set(keys)) == len(keys)
    assert all(1 <= k <= 5000 for k in keys)


def test_random_int_never_reissues_after_exhausting_a_narrow_range():
    # Sparse-key-space contract: failing loudly beats an unbounded retry loop, and beats
    # quietly handing back a key that is already in use (which would break R3).
    gen = RandomIntKeyGen(random.Random(1), lo=1, hi=4)
    with pytest.raises(RuntimeError, match="fresh key"):
        _take(gen, 50)


def test_random_int_rejects_an_empty_range():
    with pytest.raises(ValueError, match="hi > lo"):
        RandomIntKeyGen(random.Random(0), lo=5, hi=5)


# --- determinism & independence ----------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_same_seed_yields_the_same_key_stream(kind):
    a = _take(make_keygen(kind, random.Random(11)), 50)
    b = _take(make_keygen(kind, random.Random(11)), 50)
    assert a == b


@pytest.mark.parametrize("kind", ["uuid", "random"])
def test_different_seeds_yield_different_key_streams(kind):
    a = _take(make_keygen(kind, random.Random(11)), 50)
    b = _take(make_keygen(kind, random.Random(12)), 50)
    assert a != b


@pytest.mark.parametrize("kind", KINDS)
def test_per_table_generators_are_independent(kind):
    # Each (table, PK column) owns its own generator; two tables must not share a counter
    # or consume each other's keys. Sequential streams may legitimately COINCIDE -- what
    # matters is that neither table's stream is disturbed by the other's draws.
    rng = random.Random(5)
    table_a, table_b = make_keygen(kind, rng), make_keygen(kind, rng)
    interleaved_a, interleaved_b = [], []
    for _ in range(20):
        interleaved_a.append(table_a.next())
        interleaved_b.append(table_b.next())
    assert len(set(interleaved_a)) == 20
    assert len(set(interleaved_b)) == 20
    if kind == "sequential":
        assert interleaved_a == interleaved_b == list(range(1, 21))


def test_make_keygen_rejects_an_unknown_strategy():
    with pytest.raises(ValueError, match="unknown key strategy"):
        make_keygen("snowflake", random.Random(0))


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("sequential", SequentialKeyGen),
        ("uuid", UuidKeyGen),
        ("random", RandomIntKeyGen),
    ],
)
def test_make_keygen_builds_the_named_strategy(kind, expected):
    assert isinstance(make_keygen(kind, random.Random(0)), expected)
    assert isinstance(make_keygen(kind.upper(), random.Random(0)), expected)
