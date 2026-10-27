from __future__ import annotations

import time
from collections import Counter

from livetest.assertions import AssertionFailed, NotYetReadable
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


def _counts(admin) -> bool:
    """Whether this admin's `count_rows` counts exactly the rows its `select_rows` returns."""
    return bool(getattr(admin, "diff_counts_rows", False))


def _target_behind(admins: dict, spec: dict, n_src: int, n_want: int | None) -> str | None:
    """Why the target is certainly behind, from its row count alone; None to read it in full.

    ⚠ Measured 2026-10-27: a full read of a 20M-row PostgreSQL target took 485-581 s, so a poll
    that reads everything measures the check, not the writer. A count is cheap and, where it
    differs, proves the full read would fail. Only admins whose `count_rows` counts the rows
    `select_rows` returns opt in (`diff_counts_rows`); a count that fails is no evidence, and the
    full read decides as before. `n_want` (distinct source rows) is None until the source has been
    read; a non-exact spec always has it.
    """
    admin = _endpoint_admin(admins, spec, "target")
    if not _counts(admin):
        return None
    try:
        n = admin.count_rows(spec["target"])
    except Exception:
        return None
    if spec.get("exact"):
        if n != n_src:
            return f"{spec['target']} holds {n} rows, {spec['source']} {n_src}"
    elif n_want is not None and n < n_want:
        return f"{spec['target']} has not caught up to {spec['source']}: |src|={n_want} |tgt|<={n}"
    return None


def _evaluate(admins: dict, spec: dict, src_rows, want, *, count_first: bool = True):
    expected = {"kind": "rows", "rows": src_rows}
    behind = _target_behind(admins, spec, len(src_rows), len(want)) if count_first else None
    if behind is not None:
        return False, behind, expected, None
    try:
        tgt_rows = _folded(_endpoint_admin(admins, spec, "target").select_rows(spec["target"]), spec)
    except NotYetReadable as e:
        return False, f"{spec['target']} not readable yet: {e}", expected, None
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


def _read_source(admins: dict, spec: dict, deadline: float, timeout: int, poll: float) -> list:
    while True:
        try:
            return _folded(_endpoint_admin(admins, spec, "source").select_rows(spec["source"]), spec)
        except NotYetReadable as e:
            if time.monotonic() >= deadline:
                raise AssertionFailed(
                    f"diff source {spec['source']} not readable within {timeout}s: {e}", []) from e
            time.sleep(poll or 0.05)


def _source_count(admins: dict, spec: dict) -> int | None:
    """The source's row count, when an `exact` spec may poll on counts before reading it; else None.

    ⚠ A 20M-row source took minutes to read, and the first poll waited for it: a writer that
    finished meanwhile measured 0.14 s. An exact spec needs only the count until the target's
    count matches, so the full source read moves to that poll.
    """
    if not spec.get("exact"):
        return None
    src, tgt = _endpoint_admin(admins, spec, "source"), _endpoint_admin(admins, spec, "target")
    if not (_counts(src) and _counts(tgt)):
        return None
    try:
        return src.count_rows(spec["source"])
    except Exception:
        return None


def assert_diff(admins: dict, specs: list[dict], timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None,
                 clock_start: float | None = None) -> list[dict]:
    """Poll until every target holds its source; one passed record per spec, with `elapsed_s`.

    `admins` maps db-name -> admin (each exposing select_rows). Every source must be non-empty (an
    empty source asserts nothing). Source tables are seeded once and static, so caching the source
    rows is safe; the target is polled.

    §85.3. `elapsed_s` runs from `clock_start` (a `time.monotonic()` value: the harness passes when
    the app was started or its last post_start input landed) to the poll that passed. Without it the
    clock starts at the first poll. When the FIRST poll already passes, the target was equal before
    anything measured it, so the time is only an upper bound and the record says so
    (`elapsed_upper_bound`).
    """
    read_deadline = time.monotonic() + timeout
    # Per spec: [rows or None, distinct rows or None, row count]. An exact spec on counting admins
    # starts with its count only (`_source_count`).
    sources = []
    for s in specs:
        n = _source_count(admins, s)
        if n is not None:
            if n == 0:
                raise AssertionError(f"source {s['source']} is empty; nothing to propagate")
            sources.append([None, None, n])
            continue
        src_rows = _read_source(admins, s, read_deadline, timeout, poll)
        want = distinct_set(src_rows)
        if not want:
            raise AssertionError(f"source {s['source']} is empty; nothing to propagate")
        sources.append([src_rows, want, len(src_rows)])
    started = time.monotonic()
    zero = started if clock_start is None else clock_start
    deadline = started + timeout
    first = True

    def evaluate(s, src, *, count_first=True):
        if src[0] is None:
            behind = _target_behind(admins, s, src[2], None) if count_first else None
            if behind is not None:
                return False, behind, {"kind": "count", "count": src[2]}, None
            src[0] = _read_source(admins, s, deadline, timeout, poll)
            src[1] = distinct_set(src[0])
            return _evaluate(admins, s, src[0], src[1], count_first=False)   # just counted
        return _evaluate(admins, s, src[0], src[1], count_first=count_first)

    while True:
        if status_probe:
            status_probe()
        # The time reported is when the passing poll began reading its LAST spec: every target
        # was equal by then or during its own read, and a full read of millions of rows must not
        # count as writer time. Taken per spec, since earlier specs' reads can take minutes.
        results, observed = [], time.monotonic()
        for s, src in zip(specs, sources):
            observed = time.monotonic()
            results.append(evaluate(s, src))
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None:
            # ⚠ RESOLUTION IS THE POLL INTERVAL plus one failed poll. At the 2.0s default this is
            # ±2s, which is useless for comparing two writers that finish within seconds of each
            # other -- a paired measurement must lower `poll` or the number it reports is
            # quantisation. A failed poll is a count where `diff_counts_rows` allows it.
            elapsed = observed - zero
            upper = first and clock_start is not None
            return [
                build_assertion_result(type="diff", status="passed", spec=s, target=s["target"], db=db,
                                        detail="ok", expected=exp, actual=act, elapsed_s=elapsed,
                                        elapsed_upper_bound=upper)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
        first = False
        if time.monotonic() >= deadline:
            # A target the count rejected was never read: read it once now, so the failure
            # carries its rows and, under `exact`, the missing and extra counts.
            results = [r if r[3] is not None or r[0] else evaluate(s, src, count_first=False)
                       for r, s, src in zip(results, specs, sources)]
            detail = next((d for ok, d, _, _ in results if not ok), detail)
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
