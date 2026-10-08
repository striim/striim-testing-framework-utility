from __future__ import annotations

import json
import time
from pathlib import Path

from livetest.assertions import AssertionFailed
from livetest.assertions.expected import load_expected_text
from livetest.resultschema import build_assertion_result


class JsonSpecError(Exception):
    pass


def parse_json_specs(raw: list) -> list:
    if not isinstance(raw, list):
        raise JsonSpecError("'assert.json' must be a list of specs")
    for spec in raw:
        if not isinstance(spec, dict):
            raise JsonSpecError(f"json spec must be an object: {spec!r}")
        for field in ("target", "db", "column", "match"):
            if not isinstance(spec.get(field), str) or not spec.get(field):
                raise JsonSpecError(f"json spec needs a string '{field}': {spec!r}")
        key = spec.get("key")
        if isinstance(key, str):
            spec["key"] = [key]
        elif isinstance(key, list) and key and all(isinstance(k, str) for k in key):
            pass
        else:
            raise JsonSpecError(
                f"json spec {spec.get('target')!r} needs a 'key' that is a string or non-empty list "
                f"of strings: {key!r}")
    return raw


# --------------------------------------------------------------------------------------
# Pure comparator (no DB, no I/O) -- path-scoped semantic JSON diff.
# --------------------------------------------------------------------------------------


def _canon(v):
    if isinstance(v, dict):
        return {k: _canon(v[k]) for k in v}
    if isinstance(v, list):
        return sorted((_canon(x) for x in v), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    return v


def _diff_path(actual, expected, path: str = ""):
    # Returns None if actual/expected are semantically equal, else a human string naming
    # the first differing location (dict key-set mismatch, list length mismatch, or a
    # scalar/type mismatch).
    if isinstance(actual, dict) and isinstance(expected, dict):
        akeys, ekeys = set(actual.keys()), set(expected.keys())
        for k in ekeys - akeys:
            return f"{path or '<root>'}: missing key {k!r}"
        for k in akeys - ekeys:
            return f"{path or '<root>'}: unexpected key {k!r}"
        for k in ekeys:
            sub = f"{path}.{k}" if path else k
            d = _diff_path(actual[k], expected[k], sub)
            if d is not None:
                return d
        return None

    if isinstance(actual, list) and isinstance(expected, list):
        a_sorted = sorted((_canon(x) for x in actual), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        e_sorted = sorted((_canon(x) for x in expected), key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
        if len(a_sorted) != len(e_sorted):
            return f"{path or '<root>'}: array length {len(a_sorted)} != {len(e_sorted)}"
        for i, (a, e) in enumerate(zip(a_sorted, e_sorted)):
            d = _diff_path(a, e, f"{path}[{i}]")
            if d is not None:
                return d
        return None

    if actual != expected:
        return f"{path or '<root>'} (expected {expected!r}, got {actual!r})"
    return None


def json_equal(actual, expected):
    d = _diff_path(actual, expected)
    return (d is None, d)


def _plain(v):
    # The read path may hand back a JsonObject-like value (google-cloud-spanner) rather
    # than a plain dict/list; also DatetimeWithNanoseconds for TIMESTAMP columns.
    # Use json.dumps with default=str to serialize non-JSON-serializable types like
    # DatetimeWithNanoseconds, then json.loads back to plain types.
    if isinstance(v, (dict, list)):
        try:
            return json.loads(json.dumps(v, default=str))
        except (TypeError, ValueError):
            return v
    try:
        # For scalar types, try json.dumps with default=str to handle DatetimeWithNanoseconds
        return json.loads(json.dumps(v, default=str))
    except (TypeError, ValueError):
        return v


# --------------------------------------------------------------------------------------
# Golden loading
# --------------------------------------------------------------------------------------


def _load_expected(spec: dict, basedir, tokens: dict | None = None) -> dict:
    path = Path(basedir) / spec["match"]
    try:
        text = load_expected_text(path, tokens)
    except FileNotFoundError as e:
        raise JsonSpecError(f"{spec['target']}: golden file not found: {path}") from e
    try:
        rows = json.loads(text)
    except ValueError as e:
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} is not valid JSON: {e}") from e
    if not isinstance(rows, list):
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} must be a JSON array of row objects")
    if not rows:
        raise JsonSpecError(f"{spec['target']}: golden {spec['match']} is empty; must contain at least one row")

    expected = {}
    for elem in rows:
        if not isinstance(elem, dict):
            raise JsonSpecError(f"{spec['target']}: golden {spec['match']} elements must be objects")
        try:
            key_tuple = tuple(str(elem[k]) for k in spec["key"])
        except KeyError as e:
            raise JsonSpecError(
                f"{spec['target']}: golden {spec['match']} element missing key field {e.args[0]!r}: {elem!r}") from e
        if spec["column"] not in elem:
            raise JsonSpecError(
                f"{spec['target']}: golden {spec['match']} element missing column field "
                f"{spec['column']!r}: {elem!r}")
        expected[key_tuple] = elem[spec["column"]]
    return expected


# --------------------------------------------------------------------------------------
# Assertion
# --------------------------------------------------------------------------------------


def _evaluate(admin, spec: dict, expected: dict):
    target = spec["target"]
    rows = admin.select_json_rows(target, spec["key"], spec["column"])
    actual = {key_tuple: _plain(value) for key_tuple, value in rows}

    akeys, ekeys = set(actual.keys()), set(expected.keys())
    for k in ekeys - akeys:
        return False, f"{target}: missing key {'.'.join(spec['key'])}={k!r}", expected, actual
    for k in akeys - ekeys:
        return False, f"{target}: unexpected key {'.'.join(spec['key'])}={k!r}", expected, actual

    for k in ekeys:
        ok, diff = json_equal(actual[k], expected[k])
        if not ok:
            return False, f"{target} key={k}: {diff}", expected, actual

    return True, None, expected, actual


def _format_json_failure(results: list, expecteds: list, specs: list = None) -> str:
    """Format expected vs actual JSON data for test failure output."""
    lines = []
    for i, ((ok, d, exp, act), exp_dict) in enumerate(zip(results, expecteds)):
        if not ok:
            lines.append("")
            # Add column name if specs provided
            if specs and i < len(specs):
                col_info = f" [column: {specs[i]['column']}]"
            else:
                col_info = ""
            lines.append(d + col_info)  # main error message with column name
            lines.append("")
            lines.append("  EXPECTED:")
            for k, v in sorted(exp_dict.items()):
                lines.append(f"    {k}: {json.dumps(v, indent=6)}")
            lines.append("")
            lines.append("  ACTUAL:")
            for k, v in sorted(act.items()):
                lines.append(f"    {k}: {json.dumps(v, indent=6)}")
    return "\n".join(lines)


def assert_json(admin, specs: list, basedir, timeout: int, poll: float = 2.0,
                 status_probe=None, *, db: str | None = None, progress=None,
                 tokens: dict | None = None) -> list:
    db_label = db
    if not hasattr(admin, "select_json_rows"):
        raise JsonSpecError(f"assert.json db={db_label!r} is not a JSON-capable target (need a Spanner admin)")

    expecteds = [_load_expected(s, basedir, tokens) for s in specs]
    deadline = time.monotonic() + timeout
    while True:
        if status_probe:
            status_probe()
        results = [_evaluate(admin, s, exp) for s, exp in zip(specs, expecteds)]
        detail = next((d for ok, d, _, _ in results if not ok), None)
        if detail is None:
            return [
                build_assertion_result(type="json", status="passed", spec=s, target=s["target"], db=db,
                                        detail="ok", expected=None, actual=None)
                for s in specs
            ]
        if time.monotonic() >= deadline:
            records = [
                build_assertion_result(type="json", status="passed" if ok else "failed", spec=s,
                                        target=s["target"], db=db, detail="ok" if ok else d,
                                        expected=None, actual=None)
                for s, (ok, d, _, _) in zip(specs, results)
            ]
            detailed_msg = _format_json_failure(results, expecteds, specs)
            # Print detailed message to console so it's visible in test output
            print(f"\n{'='*80}\nJSON ASSERTION FAILURE DETAILS:{detailed_msg}\n{'='*80}\n")
            raise AssertionFailed(f"json assertion not satisfied within {timeout}s:{detailed_msg}", records)
        if progress:
            try:
                progress(timeout - (deadline - time.monotonic()), float(timeout))
            except Exception:
                pass
        time.sleep(poll or 0.05)   # floor avoids a busy-wait when poll=0 with a positive timeout
