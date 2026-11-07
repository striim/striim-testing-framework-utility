"""Bigtable `assert.target` -- the live mirror of inttest.dbroutes.assert_target / read_cells.

A live manifest's `assert` block dispatches a `bigtable` route here: it reads the WHOLE table back
via `BigtableAdmin.read_all` (`CellsColumnLimitFilter(num_cells=1)` -- the newest cell per
(family, qualifier), values rendered hex-for-bytes -- the same snapshot inttest.dbroutes.read_cells
records to `expected/*.json`) and compares the row list to a hand-written `expected` list of
`{"row": <hex>, "cells": {"<family>:<qualifier>": <hex>}}` rows.

Bigtable has NO SQL -- a table is read whole and compared, never queried -- so this is exactly the
branch the live `assert.target` tier was missing: the int tier already wires it through
`inttest.dbroutes.assert_target` (dbroutes.py:737), which is why the int smoke (§4.1) works today
while the live smoke (§4.2) needs this mirror (plan §3).

It polls like the other service asserts (a `SetCell` write can be async-consistent before the read
sees it) and, on a timeout, raises AssertionFailed carrying the per-spec sidecar records -- the same
shape as assert_gcs / assert_json -- so a failed assert still records what was actually read.
"""
from __future__ import annotations

import time

from livetest.assertions import AssertionFailed
from livetest.resultschema import build_assertion_result


class BigtableSpecError(Exception):
    pass


def parse_bigtable_specs(raw: list) -> list[dict]:
    """A `bigtable` assert is a list of `{table, expected}` specs; `expected` is the row list the
    table is read whole to and compared against (the `expected/*.json` the int snapshot records)."""
    if not isinstance(raw, list):
        raise BigtableSpecError("'assert.bigtable' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict) or not spec.get("table") or "expected" not in spec:
            raise BigtableSpecError(
                f"bigtable spec needs 'table' and 'expected' (a row list): {spec!r}")
        if not isinstance(spec["expected"], list):
            raise BigtableSpecError(
                f"bigtable spec {spec.get('table')!r} 'expected' must be a row list, got "
                f"{type(spec['expected']).__name__}")
    return raw


def _evaluate(admin, spec: dict):
    """Read the table whole and compare to `expected`; also return the expected/actual row-list
    snapshots for the sidecar record. Mirrors inttest.dbroutes.assert_target: the comparison is on
    the `read_all` row list (inttest wraps both sides identically as `{table, rows}`, equivalent)."""
    got = list(admin.read_all(spec["table"]))
    want = list(spec["expected"])
    expected = {"kind": "rows", "rows": want}
    actual = {"kind": "rows", "rows": got}
    if got != want:
        detail = (f"{spec['table']}: read {len(got)} row(s), expected {len(want)}; "
                  f"actual {got} != expected {want}")
        return False, detail, expected, actual
    return True, None, expected, actual


def _check_spec(admin, spec: dict):
    ok, detail, _, _ = _evaluate(admin, spec)
    return None if ok else detail


def assert_bigtable(admin, specs: list[dict], timeout: int, poll: float = 2.0,
                    status_probe=None, *, db: str | None = None,
                    progress=None) -> list[dict]:
    """Read each spec's table whole and compare to its `expected`; poll until every spec is
    satisfied or `timeout` elapses. Returns one passed record per spec; on a timeout raises
    AssertionFailed carrying the per-spec records so the sidecar still records what was read.

    The signature mirrors assert_gcs / assert_json so the plugin's `assert.target` dispatch can call
    it as `assert_bigtable(admins[route]['admin'], specs, timeout=m.timeout, status_probe=…,
    progress=…, db=<route>)` -- the one wiring the live smoke (§4.2) needs beside this module."""
    deadline = time.monotonic() + timeout
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(admin, s) for s in specs]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None:
            return [build_assertion_result(type="bigtable", status="passed", spec=s,
                                           target=s["table"], db=db, detail="ok",
                                           expected=exp, actual=act)
                    for s, (ok, d, exp, act) in zip(specs, results)]
        if time.monotonic() >= deadline:
            records = [build_assertion_result(type="bigtable", status="passed" if ok else "failed",
                                              spec=s, target=s["table"], db=db,
                                              detail="ok" if ok else d, expected=expected, actual=actual)
                      for s, (ok, d, expected, actual) in zip(specs, results)]
            raise AssertionFailed(
                f"bigtable assertion not satisfied within {timeout}s: {detail}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
