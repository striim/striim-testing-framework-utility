from __future__ import annotations

import csv
import io
import json
import time
from collections import Counter
from pathlib import Path

from livetest.assertions import AssertionFailed
from livetest.assertions.data import apply_null, distinct_set, normalized_row, sorted_rows_for_display, strip_absent   # reuse the exact distinct-set compare
from livetest.assertions.expected import load_expected_text
from livetest.resultschema import build_assertion_result


class FileSpecError(Exception):
    pass


_PROJECTIONS = ("data", "userdata", "before", "all")


def parse_file_specs(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        raise FileSpecError("'assert.file' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict) or not spec.get("path"):
            raise FileSpecError(f"file spec needs a 'path': {spec!r}")
        if not any(k in spec for k in ("match", "min_events", "events", "distinct_events")):
            raise FileSpecError(
                f"file spec {spec['path']!r} needs one of match/min_events/events/distinct_events")
        if "events" in spec:
            if "min_events" in spec:
                raise FileSpecError("file spec must choose events or min_events, not both")
            count = spec["events"]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise FileSpecError("file spec 'events' must be a positive integer")
        if "distinct_events" in spec:
            if any(k in spec for k in ("match", "min_events", "events")):
                raise FileSpecError(
                    "file spec 'distinct_events' stands alone: not with match, events or min_events")
            count = spec["distinct_events"]
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise FileSpecError("file spec 'distinct_events' must be a positive integer")
        if spec.get("project", "data") not in _PROJECTIONS:
            raise FileSpecError(
                f"file spec {spec['path']!r}: project must be one of {list(_PROJECTIONS)}")
        if "metadata" in spec and not isinstance(spec["metadata"], list):
            raise FileSpecError(f"file spec {spec['path']!r}: 'metadata' must be a list of keys")
        if "keys" in spec and not isinstance(spec["keys"], list):
            raise FileSpecError(f"file spec {spec['path']!r}: 'keys' must be a list of column names")
        _parse_ordering_specs(spec)
        if "min_events" in spec:
            try:
                min_events = int(spec["min_events"])
            except (TypeError, ValueError) as e:
                raise FileSpecError(
                    f"file spec {spec['path']!r}: 'min_events' must be an integer: {spec['min_events']!r}") from e
            if min_events <= 0:
                raise FileSpecError(
                    f"file spec {spec['path']!r}: 'min_events' must be positive, got {min_events}")
    return raw


def _parse_ordering_specs(spec: dict) -> None:
    # Three opt-in checks for a file spec. All default off, so an existing manifest
    # compares exactly as before.
    #   multiset: true      -- the golden is a MULTISET: duplicates and a missing-plus-duplicate
    #                          swap fail, where the distinct-set compare accepts both.
    #   order: [{before: {COL: V, ...}, after: {COL: V, ...}}, ...]
    #                       -- causal edges: every event matching `after` must have an earlier
    #                          event matching `before` in the file. Unrelated events may
    #                          interleave freely. Both selectors must match at least one event,
    #                          so an edge can never pass vacuously.
    #   stable_seconds: N   -- after the spec first passes, keep re-reading for N seconds; a
    #                          change that breaks it (a late extra or error event) fails the
    #                          run instead of being missed because the prefix matched.
    path = spec["path"]
    if "multiset" in spec:
        if not isinstance(spec["multiset"], bool):
            raise FileSpecError(f"file spec {path!r}: 'multiset' must be true or false")
        if spec["multiset"] and "match" not in spec:
            raise FileSpecError(f"file spec {path!r}: 'multiset' needs a 'match' golden")
    if "order" in spec:
        edges = spec["order"]
        if not isinstance(edges, list) or not edges:
            raise FileSpecError(f"file spec {path!r}: 'order' must be a non-empty list")
        for i, edge in enumerate(edges):
            if (not isinstance(edge, dict) or set(edge) != {"before", "after"}
                    or not all(isinstance(edge[k], dict) and edge[k] for k in ("before", "after"))):
                raise FileSpecError(
                    f"file spec {path!r}: order[{i}] must be {{before: {{...}}, after: {{...}}}}"
                    " with non-empty column selectors")
    if "stable_seconds" in spec:
        v = spec["stable_seconds"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise FileSpecError(f"file spec {path!r}: 'stable_seconds' must be a non-negative number")


def _selector_matches(row: dict, selector: dict) -> bool:
    got = normalized_row(row)
    want = normalized_row(selector)
    return all(k in got and got[k] == v for k, v in want.items())


def _check_order(rows: list[dict], edges: list[dict]):
    """First violated causal edge as a message, or None. rows are in file order."""
    for i, edge in enumerate(edges):
        before_idx = [n for n, r in enumerate(rows) if _selector_matches(r, edge["before"])]
        after_idx = [n for n, r in enumerate(rows) if _selector_matches(r, edge["after"])]
        if not before_idx:
            return f"order[{i}]: no event matches before={edge['before']}"
        if not after_idx:
            return f"order[{i}]: no event matches after={edge['after']}"
        first_before = before_idx[0]
        early = [n for n in after_idx if n < first_before]
        if early:
            return (f"order[{i}]: event #{early[0]} matching after={edge['after']} precedes the"
                    f" first event matching before={edge['before']} (#{first_before})")
    return None


def _has_later_value(text: str, start: int, n: int) -> bool:
    # Scan forward from `start`, skipping wrapper/comma punctuation, trying to decode an
    # *event object* at each position. Used to distinguish "the file is still being
    # written" (no event ever follows the failure) from real mid-stream corruption (a
    # later object still decodes fine, so what we choked on wasn't just a trailing
    # partial). Only a dict counts -- a partial trailing object like {"data":{"ID" still
    # contains a decodable bare string ("data"), which isn't an event and must not be
    # mistaken for one.
    dec = json.JSONDecoder()
    j = start
    while j < n:
        while j < n and text[j] in " \t\r\n,[]":
            j += 1
        if j >= n:
            return False
        try:
            obj, _ = dec.raw_decode(text, j)
        except json.JSONDecodeError:
            j += 1
            continue
        if isinstance(obj, dict):
            return True
        j += 1
    return False


def parse_json_events(text: str) -> list[dict]:
    # A JSONFormatter FileWriter emits a JSON array of event objects; a rollover produces
    # several files, so concatenated content can be [..][..] (or partially-written/unclosed
    # while active). Decode successive JSON values with raw_decode, skipping array/comma
    # punctuation — tolerant of the wrapper, of concatenation, and of a trailing partial.
    dec = json.JSONDecoder()
    out: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,[]":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError as e:
            remainder = text[i:].strip(" \t\r\n,[]")
            if remainder and _has_later_value(text, i + 1, n):
                # Something later in the stream still decodes cleanly, so this wasn't
                # just a trailing partial being written -- it's corruption in the
                # middle, and silently truncating here would drop real events.
                raise FileSpecError(
                    f"corrupt JSON event stream at byte offset {i}: {e}") from e
            break            # trailing partial object (file still being written)
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


def _project(event: dict, mode: str, meta_keys: list = (), keys: list | None = None) -> dict:
    # Flatten the requested section(s) of one event into a single {col: value} dict.
    # Enrichers (e.g. Lookup) write results to `userdata`; mappers to `data`.
    # meta_keys pulls named fields out of `metadata` (e.g. OperationName/TableName for a
    # AvroConverterOp conversion) alongside the data; null metadata values are omitted.
    # keys (optional) restricts the projected dict to just those column names -- so a
    # golden can assert enrichment values while ignoring runtime-nondeterministic columns
    # (e.g. Lookup's <lookup>_cachehit/_dbqueried flags, which depend on cache
    # warmth / async-bootstrap timing).
    # The before-image shares column names with `data`, so it is deliberately NOT folded
    # into `all` -- that would silently overwrite current-image values with the pre-image.
    # Assert the before-image with an explicit `project: before`.
    out: dict = {}
    if mode in ("data", "all") and not any(k in event for k in ("data", "before", "userdata", "metadata")):
        # A typed stream's event: JSONFormatter writes its fields at the top level, with no
        # WAEvent sections at all. Its fields are its data. A type with a field named like a
        # WAEvent section (data, before, userdata, metadata) is read as a WAEvent instead.
        out.update(event)
    if mode in ("data", "all"):
        out.update(event.get("data") or {})
    if mode in ("userdata", "all"):
        out.update(event.get("userdata") or {})
    if mode == "before":
        out.update(event.get("before") or {})
    meta = event.get("metadata") or {}
    for k in meta_keys:
        if meta.get(k) is not None:
            out[k] = meta[k]
    if keys is not None:
        out = {k: v for k, v in out.items() if k in keys}
    return out


def _load_golden_rows(basedir, spec: dict, tokens: dict | None = None) -> list[dict]:
    path = Path(basedir) / spec["match"]
    try:
        text = load_expected_text(path, tokens)
    except FileNotFoundError as e:
        raise FileSpecError(f"{spec['path']}: golden file not found: {path}") from e
    # strip_absent lets one golden cover records of different shapes -- see its docstring
    # and `_project`, which keeps only the keys a record carries.
    # Two markers, two halves of the same problem: <absent> drops the key so the row can
    # describe a record that lacks it, <null> makes the cell a real None so it can describe
    # one that carries it with a null value (8.128).
    return apply_null(strip_absent([dict(r) for r in csv.DictReader(io.StringIO(text))]))


def _load_golden(basedir, spec: dict, tokens: dict | None = None):
    rows = _load_golden_rows(basedir, spec, tokens)
    want = distinct_set(rows)
    if not want:
        raise FileSpecError(
            f"{spec['path']}: golden {spec['match']} is empty; a match spec must expect >=1 event")
    if frozenset() in want and spec.get("keys") is None:
        # A stripped-empty row can only ever match an event that projects to {}. `_evaluate`
        # keeps events by the UNRESTRICTED projection, so without `keys` every kept event
        # projects non-empty and frozenset() can never appear -- the golden is IMPOSSIBLE, not
        # vacuous, and would fail only after the full assertion timeout with a diff naming no
        # cause. Refuse it here, and say which of the two problems it is.
        raise FileSpecError(
            f"{spec['path']}: golden {spec['match']} uses <absent> but the spec sets no"
            " `keys:`, so it can never match: without `keys` every event that is compared"
            " projects at least one column, and an all-<absent> row matches only an empty"
            " projection. Add the `keys:` the <absent> row is written against, or drop the"
            " row.")
    if want == {frozenset()}:
        # Every row stripped to nothing -- i.e. the golden is all <absent>. That is not the
        # same as an EMPTY golden, so the guard above cannot see it, and it asserts only
        # "some event carries none of the projected keys". An unenriched record projects to
        # {} too, so such a spec stays green through a TOTAL enrichment failure. The <absent>
        # marker made this shape reachable; it is vacuous, and refused for the same reason an
        # empty golden is.
        raise FileSpecError(
            f"{spec['path']}: golden {spec['match']} strips to nothing -- every row is"
            f" all-<absent>. That asserts only that some event carries none of {spec.get('keys')},"
            " which an unenriched record satisfies, so it would pass through a total"
            " enrichment failure. Pair the <absent> row with at least one row asserting a"
            " real value.")
    return rows, want


def _evaluate(read_fn, spec: dict, golden_rows, want):
    # Mirrors _check_spec's pass/fail decision but also returns the expected/actual
    # snapshot for the sidecar record. _check_spec itself stays untouched (existing
    # direct callers/tests rely on its 3-arg signature and str|None return).
    events = parse_json_events(read_fn(spec["path"]))
    expected = actual = None

    if "events" in spec:
        expected = {"kind": "count", "count": spec["events"]}
        actual = {"kind": "count", "count": len(events)}
        if len(events) != spec["events"]:
            return False, f"{spec['path']}: events={len(events)}, want events={spec['events']}", expected, actual

    if "min_events" in spec:
        expected = {"kind": "count", "count": int(spec["min_events"])}
        actual = {"kind": "count", "count": len(events)}
        if len(events) < int(spec["min_events"]):
            return False, f"{spec['path']}: events={len(events)}, want min_events>={spec['min_events']}", expected, actual

    if "distinct_events" in spec:
        # Distinct PROJECTED rows (project/metadata/keys apply): a replayed duplicate does not
        # count, a missing copy does -- the count a recovery test wants when the writer is
        # at-least-once (FileWriter re-writes what followed the app checkpoint on restart).
        mode = spec.get("project", "data")
        mkeys = spec.get("metadata", [])
        keys = spec.get("keys")
        projected = [_project(e, mode, mkeys, keys) for e in events if _project(e, mode)]
        distinct = len(distinct_set(projected))
        expected = {"kind": "count", "count": spec["distinct_events"]}
        actual = {"kind": "count", "count": distinct}
        if distinct != spec["distinct_events"]:
            return False, (f"{spec['path']}: distinct projected events={distinct}"
                           f" (events={len(events)}), want distinct_events={spec['distinct_events']}"), expected, actual

    if want is not None:
        mode = spec.get("project", "data")
        mkeys = spec.get("metadata", [])
        keys = spec.get("keys")
        # A data assertion counts only events with an actual row payload. A boundary
        # event (a AvroConverterOp BEGIN/COMMIT: no data/userdata) is skipped even when it
        # carries metadata — so projecting a metadata key doesn't resurrect it. The
        # non-metadata projection decides "has payload"; the full one (with metadata) is
        # the row.
        projected = [_project(e, mode, mkeys, keys) for e in events if _project(e, mode)]
        got = distinct_set(projected)
        expected = {"kind": "rows", "rows": golden_rows}
        actual = {"kind": "rows", "rows": projected}
        if got != want:
            detail = (f"{spec['path']}: event set {sorted_rows_for_display(got)} "
                       f"!= golden {sorted_rows_for_display(want)}")
            return False, detail, expected, actual
        if spec.get("multiset") and golden_rows is not None:
            got_ms = Counter(frozenset(normalized_row(r).items()) for r in projected)
            want_ms = Counter(frozenset(normalized_row(r).items()) for r in golden_rows)
            if got_ms != want_ms:
                extra = got_ms - want_ms
                missing = want_ms - got_ms
                detail = (f"{spec['path']}: multiset differs from golden; surplus "
                          f"{sorted_rows_for_display(list(extra.elements()))}, missing "
                          f"{sorted_rows_for_display(list(missing.elements()))}")
                return False, detail, expected, actual

    if "order" in spec:
        mode = spec.get("project", "data")
        ordered = [_project(e, mode, spec.get("metadata", [])) for e in events]
        problem = _check_order(ordered, spec["order"])
        if problem:
            return False, f"{spec['path']}: {problem}", expected, actual

    return True, None, expected, actual


def _check_spec(read_fn, spec: dict, want):
    ok, detail, _, _ = _evaluate(read_fn, spec, None, want)
    return None if ok else detail


def _format_failure_detail(results: list) -> str:
    """Format expected vs actual data for test failure output."""
    lines = []
    for ok, d, exp, act in results:
        if not ok:
            lines.append("")
            lines.append(d)  # main error message
            if exp and act:
                lines.append("")
                if exp.get("kind") == "rows" and act.get("kind") == "rows":
                    exp_rows = exp.get("rows", [])
                    act_rows = act.get("rows", [])

                    # Normalized, i.e. exactly what the comparison sees -- see normalized_row.
                    lines.append("  EXPECTED ROWS (as compared):")
                    if exp_rows:
                        for row_dict in exp_rows:
                            lines.append(f"    {normalized_row(row_dict)}")
                    else:
                        lines.append("    (empty)")

                    lines.append("")
                    lines.append("  ACTUAL ROWS (as compared):")
                    if act_rows:
                        for row_dict in act_rows:
                            lines.append(f"    {normalized_row(row_dict)}")
                    else:
                        lines.append("    (empty)")

                    # Show missing and extra columns
                    exp_cols = {col for row in exp_rows for col in row.keys()}
                    act_cols = {col for row in act_rows for col in row.keys()}
                    missing_cols = exp_cols - act_cols
                    extra_cols = act_cols - exp_cols

                    if missing_cols:
                        lines.append("")
                        lines.append("  MISSING COLUMNS FROM ACTUAL:")
                        for col in sorted(missing_cols):
                            lines.append(f"    {col}")

                    if extra_cols:
                        lines.append("")
                        lines.append("  EXTRA COLUMNS IN ACTUAL:")
                        for col in sorted(extra_cols):
                            lines.append(f"    {col}")

                    # Also show row-level mismatches (different values, not missing columns)
                    exp_set = distinct_set(exp_rows)
                    act_set = distinct_set(act_rows)
                    missing_rows = exp_set - act_set
                    extra_rows = act_set - exp_set

                    # Filter out mismatches that are only due to missing/extra columns
                    def filter_structural_only(mismatch_rows, other_set, cols_to_remove):
                        real_diffs = set()
                        for mismatch_row in mismatch_rows:
                            # Remove tuples with structural difference columns
                            mismatch_filtered = {(k, v) for k, v in mismatch_row if k not in cols_to_remove}
                            # Check if this matches any row in the other set after same filtering
                            found_match = False
                            for other_row in other_set:
                                other_filtered = {(k, v) for k, v in other_row if k not in cols_to_remove}
                                if mismatch_filtered == other_filtered:
                                    found_match = True
                                    break
                            if not found_match:
                                real_diffs.add(mismatch_row)
                        return real_diffs

                    missing_rows = filter_structural_only(missing_rows, act_set, missing_cols | extra_cols)
                    extra_rows = filter_structural_only(extra_rows, exp_set, missing_cols | extra_cols)

                    if missing_rows:
                        lines.append("")
                        lines.append("  MISSING ROWS (different values):")
                        for row in sorted_rows_for_display(missing_rows):
                            lines.append(f"    {dict(row)}")

                    if extra_rows:
                        lines.append("")
                        lines.append("  EXTRA ROWS (different values):")
                        for row in sorted_rows_for_display(extra_rows):
                            lines.append(f"    {dict(row)}")
                elif exp.get("kind") == "count" and act.get("kind") == "count":
                    lines.append(f"  EXPECTED: {exp.get('count')} events")
                    lines.append(f"  ACTUAL: {act.get('count')} events")
    return "\n".join(lines)


def assert_file(read_fn, specs: list[dict], basedir, timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None,
                 tokens: dict | None = None) -> list[dict]:
    # read_fn(path) -> the server-file content for that path, its rollover files joined. Polls: the file is
    # written asynchronously (flushpolicy), so wait until it matches or the timeout hits.
    goldens = [(_load_golden(basedir, s, tokens)
                if "match" in s else (None, None)) for s in specs]
    deadline = time.monotonic() + timeout
    stable_for = max((float(s.get("stable_seconds", 0)) for s in specs), default=0.0)
    passed_at = None
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(read_fn, s, rows, want) for s, (rows, want) in zip(specs, goldens)]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is not None and passed_at is not None:
            # It passed, then stopped passing: something arrived after the matching prefix.
            records = [
                build_assertion_result(type="file", status="passed" if ok else "failed", spec=s,
                                        target=s["path"], db=db, detail="ok" if ok else d,
                                        expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
            raise AssertionFailed(
                "file assertion passed, then failed within its stable_seconds window (a late"
                f" event changed the result):{_format_failure_detail(results)}", records)
        if detail is None and stable_for > 0:
            now = time.monotonic()
            if passed_at is None:
                passed_at = now
            if now - passed_at < stable_for:
                time.sleep(min(poll or 0.05, max(0.0, stable_for - (now - passed_at))))
                continue
        if detail is None:
            return [
                build_assertion_result(type="file", status="passed", spec=s, target=s["path"], db=db,
                                        detail="ok", expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="file", status="passed" if ok else "failed", spec=s,
                                        target=s["path"], db=db, detail="ok" if ok else d,
                                        expected=exp, actual=act)
                for s, (ok, d, exp, act) in zip(specs, results)
            ]
            detailed_msg = _format_failure_detail(results)
            raise AssertionFailed(f"file assertion not satisfied within {timeout}s:{detailed_msg}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)
