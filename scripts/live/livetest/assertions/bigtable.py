"""Bigtable table comparison for the live tier: the `assert: bigtable:` tier.

Each spec is `{table, expected}` (plus an optional `db` route, default bigtable-target); the plugin
reads the table through that route's admin and dispatches here. The comparison: read the WHOLE table back via `BigtableAdmin.read_all` (`CellsColumnLimitFilter(num_cells=1)` -- the newest cell per
(family, qualifier), values rendered hex-for-bytes -- the same snapshot inttest.dbroutes.read_cells
records to `expected/*.json`) and compares the row list to a hand-written `expected` list of
`{"row": <hex>, "cells": {"<family>:<qualifier>": <hex>}}` rows.

Bigtable has NO SQL -- a table is read whole and compared, never queried. The integration tier's
`inttest.dbroutes.assert_target` makes the same comparison.

It polls like the other service asserts (a `SetCell` write can be async-consistent before the read
sees it) and, on a timeout, raises AssertionFailed carrying the per-spec sidecar records -- the same
shape as assert_gcs / assert_json -- so a failed assert still records what was actually read.
"""
from __future__ import annotations

import re
import time

from livetest.assertions import AssertionFailed
from livetest.bigtablespec import ROUTES, TABLE_RULE, BigtableSpecError, is_table_name  # noqa: F401
from livetest.resultschema import build_assertion_result

_SPEC_KEYS = frozenset({"table", "db", "expected"})
_HEX_RE = re.compile(r"^(?:[0-9a-f]{2})*$")


def _check_cell_value(value, where: str) -> None:
    if isinstance(value, dict):
        if set(value) != {"prefix"} or not isinstance(value["prefix"], str) or not value["prefix"]:
            raise BigtableSpecError(f"{where}: a prefix cell is {{prefix: <non-empty text>}}, got {value!r}")
    elif not isinstance(value, str) or not _HEX_RE.match(value):
        raise BigtableSpecError(f"{where}: a cell value is lowercase hex or {{prefix: <text>}}, got {value!r}")


def parse_bigtable_specs(raw: list) -> list[dict]:
    """A `bigtable` assert is a list of `{table, expected, db}` specs; `expected` is the row list the
    table is read whole to and compared against (the `expected/*.json` the int snapshot records).
    Returns copies, `db` defaulted to bigtable-target; the manifest's own specs are not changed."""
    if not isinstance(raw, list) or not raw:
        raise BigtableSpecError("'assert.bigtable' must be a non-empty list of specs")
    out = []
    for i, spec in enumerate(raw):
        where = f"assert.bigtable[{i}]"
        if not isinstance(spec, dict) or not spec.get("table") or "expected" not in spec:
            raise BigtableSpecError(f"{where} needs 'table' and 'expected' (a row list): {spec!r}")
        unknown = sorted(set(spec) - _SPEC_KEYS)
        if unknown:
            raise BigtableSpecError(f"{where} has unknown key(s) {unknown} -- allowed: {sorted(_SPEC_KEYS)}")
        if not is_table_name(spec["table"]):
            raise BigtableSpecError(f"{where}.table must be a table name ({TABLE_RULE}), got {spec['table']!r}")
        db = spec.get("db", "bigtable-target")
        if not isinstance(db, str) or db not in ROUTES:
            raise BigtableSpecError(f"{where}.db must be one of {sorted(ROUTES)}, got {db!r}")
        expected = spec["expected"]
        if not isinstance(expected, list):
            raise BigtableSpecError(
                f"bigtable spec {spec.get('table')!r} 'expected' must be a row list, got "
                f"{type(expected).__name__}")
        for j, row in enumerate(expected):
            at = f"{where}.expected[{j}]"
            if (not isinstance(row, dict) or set(row) != {"row", "cells"} or not isinstance(row["row"], str)
                    or not _HEX_RE.match(row["row"]) or not isinstance(row["cells"], dict)):
                raise BigtableSpecError(f"{at} must be {{row: <lowercase hex>, cells: {{...}}}}, got {row!r}")
            for cell, value in row["cells"].items():
                _check_cell_value(value, f"{at}.cells.{cell}")
        out.append({**spec, "db": db, "expected": list(expected)})
    return out


def _cell_matches(want, got_hex: str) -> bool:
    """One cell's value. A plain hex string must equal the actual hex exactly. A cell given as
    {"prefix": text} matches when its bytes, read as UTF-8, start with `text`: for a value the
    writer computes from a name known only at run time (a marker holds its component's name).
    parse_bigtable_specs has already refused any other shape."""
    if isinstance(want, dict):
        try:
            return bytes.fromhex(got_hex).decode("utf-8").startswith(want["prefix"])
        except (ValueError, UnicodeDecodeError):
            return False
    return want == got_hex


def _rows_match(want: list, got: list) -> bool:
    """The same rows, in the same order: each row's key, each row's cell names, and each cell's
    value by `_cell_matches`."""
    if len(want) != len(got):
        return False
    for w, g in zip(want, got):
        if w.get("row") != g.get("row") or set(w.get("cells", {})) != set(g.get("cells", {})):
            return False
        for cell, value in w.get("cells", {}).items():
            if not _cell_matches(value, g["cells"][cell]):
                return False
    return True


def _evaluate(admin, spec: dict, timeout=None):
    """Read the table whole and compare to `expected`; also return the expected/actual row-list
    snapshots for the sidecar record. Mirrors inttest.dbroutes.assert_target: the comparison is on
    the `read_all` row list (inttest wraps both sides identically as `{table, rows}`, equivalent).
    A cell value may be {"prefix": text} (see _cell_matches)."""
    got = list(admin.read_all(spec["table"], timeout=timeout))
    want = list(spec["expected"])
    expected = {"kind": "rows", "rows": want}
    actual = {"kind": "rows", "rows": got}
    if not _rows_match(want, got):
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

    The signature mirrors assert_gcs / assert_json: the plugin calls it once per route, with that
    route's admin and the specs parse_bigtable_specs returned for it."""
    deadline = time.monotonic() + timeout
    while True:
        if status_probe:
            status_probe()
        results = []
        try:
            for s in specs:   # every read gets only what is left of the one deadline
                results.append(_evaluate(admin, s, timeout=deadline - time.monotonic()))
        except Exception as exc:
            if _expired(exc):
                raise AssertionFailed(
                    f"bigtable assertion not satisfied within {timeout}s: a read expired: {exc}",
                    _records(specs, results, db, f"read did not finish within {timeout}s: {exc}")) from exc
            if not _not_yet(exc):
                raise
            # The table is not there yet (the writer creates it) or the emulator is briefly
            # unavailable: poll on, as for rows that have not arrived.
            if time.monotonic() >= deadline:
                raise AssertionFailed(
                    f"bigtable assertion not satisfied within {timeout}s: {exc}",
                    _records(specs, results, db, f"read failed until the {timeout}s deadline: {exc}")) from exc
            time.sleep(max(0.0, min(poll or 0.05, deadline - time.monotonic())))
            if time.monotonic() >= deadline:     # no time left for another read: report this one
                raise AssertionFailed(
                    f"bigtable assertion not satisfied within {timeout}s: {exc}",
                    _records(specs, results, db, f"read failed until the {timeout}s deadline: {exc}")) from exc
            continue
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None and time.monotonic() > deadline:
            # A read that matches only after the deadline is not a pass.
            detail = f"the table matched only after the {timeout}s deadline"
            raise AssertionFailed(f"bigtable assertion not satisfied within {timeout}s: {detail}",
                                  _records(specs, results, db, detail))
        if detail is None:
            return [build_assertion_result(type="bigtable", status="passed", spec=s,
                                           target=s["table"], db=db, detail="ok",
                                           expected=exp, actual=act)
                    for s, (ok, d, exp, act) in zip(specs, results)]
        if time.monotonic() >= deadline:
            raise AssertionFailed(
                f"bigtable assertion not satisfied within {timeout}s: {detail}",
                _records(specs, results, db))
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(max(0.0, min(poll or 0.05, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            # No time left for another read, which would expire before it is sent: fail on what
            # this read showed, so the record keeps the mismatch and the rows actually read.
            raise AssertionFailed(
                f"bigtable assertion not satisfied within {timeout}s: {detail}",
                _records(specs, results, db))


def _expired(exc: BaseException) -> bool:
    """An RPC that ran out of time: the admin's own budget, or the SDK's DeadlineExceeded."""
    if isinstance(exc, TimeoutError):
        return True
    from google.api_core.exceptions import DeadlineExceeded
    return isinstance(exc, DeadlineExceeded)


def _not_yet(exc: BaseException) -> bool:
    """A read that may succeed later: the table does not exist yet, or the emulator is unavailable."""
    from google.api_core.exceptions import NotFound, ServiceUnavailable
    return isinstance(exc, (NotFound, ServiceUnavailable))


def _records(specs: list[dict], results: list, db, late: str | None = None) -> list[dict]:
    """One sidecar record per spec. A spec read in time keeps its comparison; with `late` set,
    every spec fails with that detail -- a spec never read records only what was expected."""
    records = []
    for i, s in enumerate(specs):
        if i < len(results):
            ok, d, expected, actual = results[i]
        else:
            ok, d, expected, actual = False, None, {"kind": "rows", "rows": list(s["expected"])}, None
        failed = late is not None or not ok
        records.append(build_assertion_result(
            type="bigtable", status="failed" if failed else "passed", spec=s, target=s["table"], db=db,
            detail=(late or d) if failed else "ok", expected=expected, actual=actual))
    return records
