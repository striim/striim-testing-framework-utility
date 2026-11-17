"""`setup: bigtable:` -- the manifest form both tiers load. The integration tier's copy
(inttest/bigtablespec.py) is this code, copied so `inttest` imports no `livetest`;
integration/tests/test_bigtablespec_tiers.py keeps the copies equal below this docstring."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"
# BigtableAdmin's own rule, [A-Za-z0-9_][A-Za-z0-9_.-]*, with ${TOKEN}s allowed anywhere.
TABLE_RE = re.compile(rf"^(?:[A-Za-z0-9_]|{_TOKEN})(?:[A-Za-z0-9_.\-]|{_TOKEN})*$")
FAMILY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
ROUTES = frozenset({"bigtable-target", "bigtable-source"})
GC_KINDS = frozenset({"num_versions", "age_millis", "age_seconds", "age_days"})
_SETUP_KEYS = frozenset({"bigtable"})
_ENTRY_KEYS = frozenset({"table", "db", "families", "gc", "seed"})
_ROW_KEYS = frozenset({"row", "cells"})
TABLE_RULE = "letters, digits, '_', '.', '-', or ${TOKEN}s, not starting with '.' or '-'"


class BigtableSpecError(ValueError):
    pass


@dataclass(frozen=True)
class BigtableSetup:
    """One `setup: bigtable:` entry: a table created (families, GC rules, seed rows) before the
    case's DDL, dropped at teardown. `table` carries ${TOKEN}s, rendered at run time."""
    table: str
    db: str
    families: dict                              # family -> None
    gc_rules: dict = field(default_factory=dict)
    seed: list = field(default_factory=list)    # rows: {"row": key, "cells": {"fam:qual": value}}


def is_table_name(text) -> bool:
    return isinstance(text, str) and bool(TABLE_RE.match(text))


def _gc_rule(rule, where: str):
    if rule is None:
        return None                       # no rule: the family never expires
    if not isinstance(rule, dict) or len(rule) != 1 or not set(rule) <= GC_KINDS:
        raise BigtableSpecError(f"{where} must be one of {sorted(GC_KINDS)} with a count, got {rule!r}")
    (value,) = rule.values()
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BigtableSpecError(f"{where} must be a whole number greater than 0, got {value!r}")
    return dict(rule)


def _check_bytes(text: str, what: str) -> None:
    """A seed key or value is UTF-8 text, or 0x<hex> for bytes (BigtableAdmin._as_bytes)."""
    if text.startswith("0x"):
        try:
            bytes.fromhex(text[2:])
        except ValueError:
            raise BigtableSpecError(f"{what} {text!r} starts with 0x but is not hex") from None


def _seed_rows(seed, families: dict, where: str) -> list:
    if not isinstance(seed, list):
        raise BigtableSpecError(f"{where}.seed must be a list of rows")
    for j, row in enumerate(seed):
        at = f"{where}.seed[{j}]"
        if (not isinstance(row, dict) or set(row) - _ROW_KEYS or not isinstance(row.get("row"), str)
                or not row["row"] or not isinstance(row.get("cells"), dict) or not row["cells"]):
            raise BigtableSpecError(f"{at} needs 'row' (a non-empty string) and a non-empty 'cells' mapping")
        _check_bytes(row["row"], f"{at}.row")
        if row["row"] == "0x":
            raise BigtableSpecError(f"{at}.row is an empty key")
        for cell, value in row["cells"].items():
            fam, sep, qual = str(cell).partition(":")
            if not sep or not qual or fam not in families:
                raise BigtableSpecError(
                    f"{at}.cells: {cell!r} must be '<family>:<qualifier>' with a family 'families' declares")
            if not isinstance(value, str):
                raise BigtableSpecError(f"{at}.cells.{cell} must be a string (text, or 0x<hex> for bytes)")
            _check_bytes(value, f"{at}.cells.{cell}")
    return list(seed)


def normalize_setup(raw, path) -> list:
    """The top-level `setup:` block. Only `bigtable:` exists: a list of BigtableSetup. Raises
    BigtableSpecError naming `path` and the offending key."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise BigtableSpecError(f"{path}: 'setup' must be a mapping with a 'bigtable' list, got {raw!r}")
    unknown = sorted(set(raw) - _SETUP_KEYS)
    if unknown:
        raise BigtableSpecError(f"{path}: 'setup' has unknown key(s) {unknown} -- allowed: {sorted(_SETUP_KEYS)}")
    items = raw.get("bigtable")
    if items is None:
        return []
    if not isinstance(items, list) or not items:
        raise BigtableSpecError(f"{path}: 'setup.bigtable' must be a non-empty list of table specs")
    out, seen = [], set()
    for i, item in enumerate(items):
        where = f"{path}: 'setup.bigtable[{i}]'"
        if not isinstance(item, dict):
            raise BigtableSpecError(f"{where} must be a mapping, got {item!r}")
        unknown = sorted(set(item) - _ENTRY_KEYS)
        if unknown:
            raise BigtableSpecError(f"{where} has unknown key(s) {unknown} -- allowed: {sorted(_ENTRY_KEYS)}")
        table = item.get("table")
        if not is_table_name(table):
            raise BigtableSpecError(f"{where} needs 'table' ({TABLE_RULE}), got {table!r}")
        # The table is created and dropped by name: without the run's ${TID} two parallel runs on
        # one emulator would create, write and drop the same table. (A serial integration run's
        # TID is empty; its runs are sequential.)
        if "${TID}" not in table:
            raise BigtableSpecError(
                f"{where}.table {table!r} must contain ${{TID}}, so parallel runs do not share a table")
        db = item.get("db", "bigtable-target")
        if not isinstance(db, str) or db not in ROUTES:
            raise BigtableSpecError(f"{where}.db must be one of {sorted(ROUTES)}, got {db!r}")
        if (db, table) in seen:
            raise BigtableSpecError(f"{where} names table {table!r} on {db!r} a second time")
        seen.add((db, table))
        families = item.get("families")
        if not isinstance(families, dict) or not families:
            raise BigtableSpecError(
                f"{where} needs 'families': a mapping of family name to null (GC rules go under 'gc')")
        for fam, rule in families.items():
            if not isinstance(fam, str) or not FAMILY_RE.match(fam):
                raise BigtableSpecError(f"{where}.families: {fam!r} is not a family name ({FAMILY_RE.pattern})")
            if rule is not None:
                raise BigtableSpecError(f"{where}.families.{fam} must be null; put its GC rule under 'gc'")
        gc = item.get("gc") if item.get("gc") is not None else {}
        if not isinstance(gc, dict):
            raise BigtableSpecError(f"{where}.gc must be a mapping of family to rule")
        stray = sorted(set(gc) - set(families))
        if stray:
            raise BigtableSpecError(f"{where}.gc names family(ies) {stray} that 'families' does not declare")
        gc_rules = {fam: _gc_rule(rule, f"{where}.gc.{fam}") for fam, rule in gc.items()}
        seed = _seed_rows(item.get("seed") if item.get("seed") is not None else [], families, where)
        out.append(BigtableSetup(table=table, db=db, families=dict(families), gc_rules=gc_rules, seed=seed))
    return out
