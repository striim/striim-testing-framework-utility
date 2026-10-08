from __future__ import annotations

import time
from collections import Counter

from livetest.assertions import AssertionFailed
from livetest.assertions.data import distinct_set, normalized_row
from livetest import canon as _slt_canon   # legacy-text/1 (C8.4)
from livetest.resultschema import build_assertion_result


class DiffSpecError(Exception):
    pass


def parse_diff_specs(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        raise DiffSpecError("'assert.diff' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict) or not spec.get("source") or not spec.get("target"):
            raise DiffSpecError(f"diff spec needs both 'source' and 'target': {spec!r}")
        if "exact" in spec and not isinstance(spec["exact"], bool):
            raise DiffSpecError(f"diff spec 'exact' must be a boolean: {spec!r}")
        if "fold_names" in spec and not isinstance(spec["fold_names"], bool):
            raise DiffSpecError(f"diff spec 'fold_names' must be a boolean: {spec!r}")
    return raw


def _folded(rows: list, spec: dict) -> list:
    """Rows with column names lower-cased, when the spec asks for it.

    A diff compares rows as name->value maps, so a PostgreSQL source (`id`) against an Oracle
    target (`ID`) is never equal however right the data is -- measured: the target held all
    50,000 rows at their final state and the diff timed out at 900s. `fold_names: true` is the
    opt-in for a cross-engine diff; it is not the default because a same-engine case may
    legitimately pin a column's case.
    """
    if not spec.get("fold_names"):
        return rows
    return [{str(k).lower(): v for k, v in r.items()} for r in rows]


def _endpoint_admin(admins: dict, spec: dict, endpoint: str):
    # source/target may live in different databases (e.g. Postgres source ->
    # Spanner target). `source_db`/`target_db` override the spec-wide `db`
    # (default "postgres-source"), and each resolves to its own admin.
    db = spec.get(f"{endpoint}_db", spec.get("db", "postgres-source"))
    if db not in admins:
        raise AssertionError(
            f"diff {endpoint}_db {db!r} has no admin — add the service to the test's 'requires'")
    return admins[db]


def _multiset(rows: list) -> "Counter":
    """Rows as a MULTISET, so a duplicate is a difference.

    ⚠ §127.5. `distinct_set` collapses duplicates, so a target holding a row TWICE compares equal
    to a source holding it once. That is correct for "has the target caught up" and wrong for
    §85.3's gate, which is "did these two writers put the SAME DATA there" -- a writer that
    duplicated every row would otherwise converge and report a clean time.
    """
    return Counter(frozenset(normalized_row(r).items()) for r in rows)


def _evaluate(admins: dict, spec: dict, src_rows, want):
    tgt_rows = _folded(_endpoint_admin(admins, spec, "target").select_rows(spec["target"]), spec)
    expected = {"kind": "rows", "rows": src_rows}
    actual = {"kind": "rows", "rows": tgt_rows}
    if spec.get("exact"):
        # Cardinality AND content in one comparison: a multiset differs if a row is missing,
        # extra, or repeated a different number of times. Subsumes §85.3's "identical row count
        # AND identical checksum" without a second pass over the data.
        # C8.4: the same stringified multiset, through canon's legacy-text/1 profile.
        src_ms, tgt_ms = _slt_canon.legacy_text_multiset(src_rows), _slt_canon.legacy_text_multiset(tgt_rows)
        if src_ms != tgt_ms:
            extra = sum((tgt_ms - src_ms).values())
            missing = sum((src_ms - tgt_ms).values())
            detail = (f"{spec['target']} does not hold exactly {spec['source']}: "
                      f"|src|={len(src_rows)} |tgt|={len(tgt_rows)}, "
                      f"{missing} missing, {extra} extra-or-duplicated")
            return False, detail, expected, actual
        return True, None, expected, actual
    tgt = distinct_set(tgt_rows)
    if tgt != want:
        detail = f"{spec['target']} has not caught up to {spec['source']}: |src|={len(want)} |tgt|={len(tgt)}"
        return False, detail, expected, actual
    return True, None, expected, actual


def assert_diff(admins: dict, specs: list[dict], timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None) -> list[dict]:
    # `admins` maps db-name -> admin (each exposing select_rows). Up-front: every
    # source must be non-empty (an empty source asserts nothing). Source tables are
    # seeded once and static, so caching the source rows is safe; the target is polled.
    sources = []
    for s in specs:
        src_rows = _folded(_endpoint_admin(admins, s, "source").select_rows(s["source"]), s)
        want = distinct_set(src_rows)
        if not want:
            raise AssertionError(f"source {s['source']} is empty; nothing to propagate")
        sources.append((src_rows, want))
    # §85.3. The clock starts at the FIRST poll, not at app start: everything before this --
    # deploy, compile, source read -- is outside and is not attributed to the writer. Both arms of
    # a paired comparison pay the same overheads either way, so it is the DIFFERENCE that carries
    # the meaning, never the absolute value.
    started = time.monotonic()
    deadline = started + timeout
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(admins, s, src_rows, want) for s, (src_rows, want) in zip(specs, sources)]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None:
            # ⚠ RESOLUTION IS THE POLL INTERVAL. At the 2.0s default this is ±2s, which is useless
            # for comparing two writers that finish within seconds of each other -- a paired
            # measurement must lower `poll` or the number it reports is quantisation.
            elapsed = time.monotonic() - started
            return [
                build_assertion_result(type="diff", status="passed", spec=s, target=s["target"], db=db,
                                        detail="ok", expected=exp, actual=act, elapsed_s=elapsed)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="diff", status="passed" if ok else "failed", spec=s,
                                        target=s["target"], db=db, detail="ok" if ok else d,
                                        expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
            raise AssertionFailed(f"diff not satisfied within {timeout}s: {detail}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
