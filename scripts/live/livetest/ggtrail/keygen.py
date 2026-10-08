from __future__ import annotations
import random
import uuid
from typing import Protocol

# Key-generation strategies (spec §5, R13). One generator per (table, PK column): a
# composite PK draws each column from its own stream and the TUPLE is the row identity.
#
# The invariant every strategy upholds, and the reason RandomIntKeyGen keeps an
# ever-issued set rather than just checking the live set: a key is never REISSUED after
# its row is deleted (R3). One key means one logical record, ever -- otherwise a
# downstream CDC consumer could not tell "row 7 came back" from "row 7 was updated", and
# the workload tests could not prove delete-then-never-again.


class KeyGen(Protocol):
    def next(self) -> str | int: ...


class SequentialKeyGen:
    """1, 2, 3, ... -- the default. Monotonic, so never-reissued is structural."""

    def __init__(self, start: int = 1, step: int = 1):
        if step == 0:
            raise ValueError("SequentialKeyGen step must be non-zero")
        self._next = start
        self._step = step

    def next(self) -> int:
        value = self._next
        self._next += self._step
        return value


class UuidKeyGen:
    """RFC-4122 v4 strings drawn from the seeded rng, so a run is reproducible.

    Collisions are astronomically unlikely but not impossible under a fixed seed shared
    across tables, so this still asserts uniqueness rather than assuming it.
    """

    def __init__(self, rng: random.Random):
        self._rng = rng
        self._issued: set[str] = set()

    def next(self) -> str:
        for _ in range(64):
            value = str(uuid.UUID(bytes=self._rng.randbytes(16), version=4))
            if value not in self._issued:
                self._issued.add(value)
                return value
        raise RuntimeError("UuidKeyGen exhausted 64 attempts without a fresh key")


class RandomIntKeyGen:
    """Uniform integers, unique against every key this generator has EVER issued."""

    def __init__(self, rng: random.Random, lo: int = 1, hi: int = 10**9):
        if hi <= lo:
            raise ValueError(f"RandomIntKeyGen needs hi > lo, got lo={lo} hi={hi}")
        self._rng = rng
        self._lo, self._hi = lo, hi
        self._issued: set[int] = set()

    def next(self) -> int:
        # Bounded retry: the caller's key space must stay sparse relative to the row
        # count. Failing loudly beats degrading into an unbounded loop on a tiny range.
        for _ in range(1000):
            value = self._rng.randint(self._lo, self._hi)
            if value not in self._issued:
                self._issued.add(value)
                return value
        raise RuntimeError(
            f"RandomIntKeyGen could not find a fresh key in [{self._lo},{self._hi}] after 1000 "
            f"attempts ({len(self._issued)} already issued) -- widen the range"
        )


KINDS = ("sequential", "uuid", "random")


def make_keygen(kind: str, rng: random.Random) -> KeyGen:
    kind = str(kind).strip().lower()
    if kind == "sequential":
        return SequentialKeyGen()
    if kind == "uuid":
        return UuidKeyGen(rng)
    if kind == "random":
        return RandomIntKeyGen(rng)
    raise ValueError(f"unknown key strategy {kind!r}; known strategies: {list(KINDS)}")
