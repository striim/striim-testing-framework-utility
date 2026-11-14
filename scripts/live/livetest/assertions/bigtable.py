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
        # The route whose admin reads the table; the plugin groups specs by it, as for json/file.
        spec.setdefault("db", "bigtable-target")
    return raw


def _evaluate(admin, spec: dict, timeout=None):
    """Read the table whole and compare to `expected`; also return the expected/actual row-list
    snapshots for the sidecar record. Mirrors inttest.dbroutes.assert_target: the comparison is on
    the `read_all` row list (inttest wraps both sides identically as `{table, rows}`, equivalent)."""
    got = list(admin.read_all(spec["table"], timeout=timeout))
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

    The signature mirrors assert_gcs / assert_json so a future `assert.target` dispatch could call
    it as `assert_bigtable(admins[route]['admin'], specs, timeout=m.timeout, status_probe=…,
    progress=…, db=<route>)`."""
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
